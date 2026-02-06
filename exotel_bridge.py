"""
Exotel Bridge - Bidirectional WebSocket bridge between Exotel and PersonaPlex engine.

PROTOCOL SUMMARY (Source: moshi/moshi/server.py)
================================================
Engine WebSocket Protocol (Binary frames only):
- Frame format: First byte is message type, rest is payload
  - 0x00: Handshake/keepalive (server sends on connect, client can send for keepalive)
  - 0x01: Audio payload (Opus-encoded bytes via sphn.OpusStreamWriter/Reader)
  - 0x02: Text tokens (UTF-8 encoded)
- Audio codec: Opus via sphn library at 24000 Hz
- No explicit end-of-turn marker: Engine uses silence detection (0.5s of silence frames)
- Reference: moshi/moshi/server.py lines 184-251, 263-264

Exotel Protocol:
- JSON text frames: {"event":"media","media":{"payload":"<base64>"}}
- PCM16LE mono 8kHz base64-encoded

Handles:
- Exotel WebSocket (JSON text frames): {"event":"media","media":{"payload":"<base64>"}}
- PersonaPlex WebSocket (binary frames): 0x00 handshake, 0x01 Opus audio, 0x02 text
- Audio transcoding: Exotel 8kHz PCM <-> PersonaPlex 24kHz Opus
"""
# PHASE 1: Force unbuffered output for live debugging
import sys
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import asyncio
import base64
import json
import logging
import os
import ssl
import subprocess
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import quote

import numpy as np
try:
    import sphn
except ImportError:
    sphn = None  # Optional, not used in current implementation
import websockets
from scipy import signal

# ========== LOGGING SETUP ==========
LOG_LEVEL = os.getenv("BRIDGE_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stdout
)
logger = logging.getLogger('exotel_bridge')

# ========== CONFIGURATION ==========
BRIDGE_HOST = os.getenv("BRIDGE_HOST", "0.0.0.0")
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "5050"))
ENGINE_URL = os.getenv("ENGINE_URL", "wss://127.0.0.1:8998/api/chat")
VOICE_PROMPT = os.getenv("VOICE_PROMPT", "NATF0.pt")
TEXT_PROMPT = os.getenv("TEXT_PROMPT", "You enjoy having a good conversation.")

# Audio settings
MODEL_SR = int(os.getenv("MODEL_SR", "24000"))  # PersonaPlex model sample rate
EXOTEL_SR = int(os.getenv("EXOTEL_SR", "8000"))  # Exotel PCM sample rate
AUDIO_CHUNK_MS = int(os.getenv("AUDIO_CHUNK_MS", "20"))  # 20ms chunks for Opus

# Exotel lifecycle control (default OFF - safe changes only)
EXOTEL_DRAIN_AFTER_STOP = os.getenv("EXOTEL_DRAIN_AFTER_STOP", "0") == "1"
EXOTEL_DRAIN_SECS = float(os.getenv("EXOTEL_DRAIN_SECS", "8.0"))
EXOTEL_SEND_SILENCE_WHEN_IDLE = os.getenv("EXOTEL_SEND_SILENCE_WHEN_IDLE", "0") == "1"

# Build PersonaPlex WebSocket URL
if "?" in ENGINE_URL:
    PERSONAPLEX_WS = f"{ENGINE_URL}&voice_prompt={quote(VOICE_PROMPT)}&text_prompt={quote(TEXT_PROMPT)}"
else:
    PERSONAPLEX_WS = f"{ENGINE_URL}?voice_prompt={quote(VOICE_PROMPT)}&text_prompt={quote(TEXT_PROMPT)}"

# ========== FFMPEG RESAMPLER CLASS ==========
class FfmpegOggDecoder:
    """FFmpeg subprocess for Ogg Opus → PCM24k decoding."""
    
    def __init__(self, observability=None):
        self.proc = None
        self._closed = False
        self.tag = "ogg_decoder"
        self.observability = observability
        self.write_total = 0
        self.read_total = 0
        self.stderr_task = None
        self.last_log_time = time.monotonic()
    
    def start(self):
        """Start ffmpeg Ogg→PCM decoder."""
        try:
            self.proc = subprocess.Popen(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel", "warning",
                    "-f", "ogg",
                    "-i", "pipe:0",
                    "-f", "s16le",
                    "-ar", "24000",
                    "-ac", "1",
                    "-flush_packets", "1",
                    "pipe:1"
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            
            if self.proc.poll() is not None:
                stderr = self.proc.stderr.read().decode(errors="ignore")
                raise RuntimeError(f"{self.tag} exited immediately: {stderr}")
            
            logger.info(
                f"{self.tag} started: Ogg Opus → PCM24k, "
                f"PID={self.proc.pid}, stdin_open={self.proc.stdin is not None}, "
                f"stdout_open={self.proc.stdout is not None}, stderr_open={self.proc.stderr is not None}"
            )
            
            # Start stderr reader task
            self.stderr_task = asyncio.create_task(self._stderr_reader())
        except FileNotFoundError:
            raise RuntimeError(f"ffmpeg not found. Install with: apt-get install -y ffmpeg")
        except Exception as e:
            raise RuntimeError(f"{self.tag} failed to start: {e}")
    
    async def _stderr_reader(self):
        """Continuously read and log stderr with prefix."""
        try:
            loop = asyncio.get_event_loop()
            while not self._closed and self.proc and self.proc.poll() is None:
                try:
                    line = await asyncio.wait_for(
                        loop.run_in_executor(None, self.proc.stderr.readline),
                        timeout=0.1
                    )
                    if line:
                        line_str = line.decode(errors="ignore").strip()
                        if line_str:
                            logger.warning(f"[ffmpeg-{self.tag}] {line_str}")
                    elif self.proc.poll() is not None:
                        break
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.error(f"{self.tag} stderr reader error: {e}", exc_info=True)
                    break
        except Exception as e:
            logger.error(f"{self.tag} stderr reader fatal: {e}", exc_info=True)
    
    def check_stderr(self):
        """Check for FFmpeg errors in stderr."""
        if self.proc and self.proc.stderr:
            try:
                import select
                if select.select([self.proc.stderr], [], [], 0)[0]:
                    err = self.proc.stderr.read(1024).decode(errors="ignore")
                    if err:
                        return err
            except Exception:
                pass
        return None
    
    def write(self, ogg_bytes: bytes):
        """Write Ogg bytes to decoder input."""
        if self._closed or self.proc is None:
            return False
        try:
            self.proc.stdin.write(ogg_bytes)
            self.proc.stdin.flush()
            self.write_total += len(ogg_bytes)
            if self.observability:
                self.observability.update_counter('decoder_in_bytes', bytes_delta=len(ogg_bytes))
                self.observability.update_activity('decoder_in')
            
            # Rate-limited logging
            now = time.monotonic()
            if now - self.last_log_time >= 1.0:
                logger.debug(f"{self.tag} write: total={self.write_total}B")
                self.last_log_time = now
            return True
        except BrokenPipeError:
            logger.error(f"{self.tag} write: BrokenPipeError - process may have died")
            self._closed = True
            return False
        except OSError as e:
            logger.error(f"{self.tag} write: OSError: {e}")
            self._closed = True
            return False
    
    async def read(self, nbytes: int, timeout: float = 0.1):
        """Read decoded PCM24k bytes (async)."""
        if self._closed or self.proc is None:
            return None
        try:
            loop = asyncio.get_event_loop()
            data = await asyncio.wait_for(
                loop.run_in_executor(None, self.proc.stdout.read, nbytes),
                timeout=timeout
            )
            if data:
                self.read_total += len(data)
                if self.observability:
                    self.observability.update_counter('decoder_out_pcm24k_bytes', bytes_delta=len(data))
                    self.observability.update_activity('decoder_out')
                
                # Rate-limited logging
                now = time.monotonic()
                if now - self.last_log_time >= 1.0:
                    logger.debug(f"{self.tag} read: total={self.read_total}B")
                    self.last_log_time = now
            elif self.proc.poll() is not None:
                logger.warning(f"{self.tag} read: EOF and process exited (code={self.proc.returncode})")
                self._closed = True
            return data
        except asyncio.TimeoutError:
            return b""
        except Exception as e:
            logger.error(f"{self.tag} read error: {e}", exc_info=True)
            self._closed = True
            return None
    
    def stop(self):
        """Stop and cleanup decoder."""
        self._closed = True
        
        # Cancel stderr reader task
        if self.stderr_task and not self.stderr_task.done():
            self.stderr_task.cancel()
        
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.stdout.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            except Exception:
                pass
            self.proc = None
            logger.info(f"{self.tag} stopped: write_total={self.write_total}B read_total={self.read_total}B")


class PythonResampler:
    """Python-based PCM resampler using scipy.signal.resample_poly (no FFmpeg subprocess)."""
    
    def __init__(self, in_sr: int, out_sr: int, tag: str = "resampler"):
        self.in_sr = in_sr
        self.out_sr = out_sr
        self.tag = tag
        self._closed = False
        # Calculate resampling ratio
        from fractions import Fraction
        ratio = Fraction(out_sr, in_sr)
        self.up = ratio.numerator
        self.down = ratio.denominator
        logger.info(f"{self.tag}: Python resampler initialized {in_sr}Hz -> {out_sr}Hz (ratio {self.up}/{self.down})")
    
    def start(self):
        """No-op for Python resampler (no subprocess to start)."""
        pass
    
    def resample(self, pcm_bytes: bytes) -> bytes:
        """Resample PCM16LE bytes from in_sr to out_sr."""
        if self._closed:
            return b""
        
        try:
            # Convert bytes to int16 array
            pcm_int16 = np.frombuffer(pcm_bytes, dtype=np.int16)
            
            # Resample using scipy
            pcm_resampled = signal.resample_poly(pcm_int16, self.up, self.down)
            
            # Convert back to int16 and then bytes
            pcm_resampled_int16 = pcm_resampled.astype(np.int16)
            return pcm_resampled_int16.tobytes()
        except Exception as e:
            logger.error(f"{self.tag} resample error: {e}", exc_info=True)
            return b""
    
    def stop(self):
        """No-op for Python resampler."""
        self._closed = True

class FfmpegResampler:
    """Manages ffmpeg subprocess for PCM resampling (DEPRECATED: use PythonResampler)."""
    
    def __init__(self, in_sr: int, out_sr: int, tag: str = "resampler"):
        self.in_sr = in_sr
        self.out_sr = out_sr
        self.tag = tag
        self.proc = None
        self._closed = False
        
    def start(self):
        """Start ffmpeg resampler subprocess."""
        if self.proc is not None:
            return
            
        try:
            self.proc = subprocess.Popen(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel", "error",
                    "-f", "s16le",
                    "-ar", str(self.in_sr),
                    "-ac", "1",
                    "-i", "pipe:0",
                    "-f", "s16le",
                    "-ar", str(self.out_sr),
                    "-ac", "1",
                    "pipe:1",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )

            # Verify process started
            if self.proc.poll() is not None:
                stderr = self.proc.stderr.read().decode(errors="ignore")
                raise RuntimeError(f"{self.tag} exited immediately: {stderr}")
                
            logger.info(f"{self.tag} started: {self.in_sr}Hz -> {self.out_sr}Hz")
            
            # PHASE 4: Start async stderr reader task
            self.stderr_task = asyncio.create_task(self._stderr_reader())
            
        except FileNotFoundError:
            raise RuntimeError(f"ffmpeg not found. Install with: apt-get install -y ffmpeg")
        except Exception as e:
            raise RuntimeError(f"{self.tag} failed to start: {e}")
    
    def write(self, pcm_bytes: bytes):
        """Write PCM bytes to resampler input."""
        if self._closed or self.proc is None:
            return False
        try:
            self.proc.stdin.write(pcm_bytes)
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError):
            self._closed = True
            return False
    
    async def read(self, nbytes: int, timeout: float = 1.0):
        """Read resampled PCM bytes (async) - DEPRECATED: FfmpegResampler not used anymore."""
        # This method is kept for compatibility but should not be called
        logger.warning(f"{self.tag} read() called but FfmpegResampler is deprecated")
        return b""
    
    async def _stderr_reader(self):
        """PHASE 4: Continuously read and log stderr - DEPRECATED: FfmpegResampler not used."""
        # This method is kept for compatibility but should not be called
        pass
    
    def stop(self):
        """Stop and cleanup resampler."""
        self._closed = True
        
        # Cancel stderr reader task
        if self.stderr_task and not self.stderr_task.done():
            self.stderr_task.cancel()
        
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.stdout.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            except Exception:
                pass
            self.proc = None
            logger.debug(f"{self.tag} stopped")

# ========== OBSERVABILITY MODULE ==========
class PipelineObservability:
    """Observability system for pipeline stages with counters, timestamps, and deltas."""
    
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.start_time = time.monotonic()
        self.last_heartbeat_time = self.start_time
        
        # Stage counters (bytes/frames)
        self.counters = {
            'exotel_in_frames': 0,
            'exotel_in_bytes': 0,
            'pcm8k_in_bytes': 0,
            'resample_8k_to_24k_in_bytes': 0,
            'resample_8k_to_24k_out_bytes': 0,
            'pcm24k_buffer_bytes': 0,
            'opus_encode_in_pcm_bytes': 0,
            'opus_encode_out_bytes': 0,
            'engine_out_frames': 0,
            'engine_out_bytes': 0,
            'engine_in_frames': 0,
            'engine_in_bytes': 0,
            'engine_audio_frames': 0,
            'engine_audio_bytes': 0,
            'engine_text_frames': 0,
            'decoder_in_bytes': 0,
            'decoder_out_pcm24k_bytes': 0,
            'resample_24k_to_8k_in_bytes': 0,
            'resample_24k_to_8k_out_bytes': 0,
            'exotel_out_frames': 0,
            'exotel_out_bytes': 0,
        }
        
        # Previous tick values for delta calculation
        self.prev_counters = self.counters.copy()
        
        # Last activity timestamps (monotonic)
        self.last_activity = {
            'exotel_in': self.start_time,
            'pcm8k': self.start_time,
            'resample_in': self.start_time,
            'resample_out': self.start_time,
            'encode_in': self.start_time,
            'encode_out': self.start_time,
            'engine_out': self.start_time,
            'engine_in': self.start_time,
            'decoder_in': self.start_time,
            'decoder_out': self.start_time,
            'resample_down_in': self.start_time,
            'resample_down_out': self.start_time,
            'exotel_out': self.start_time,
        }
        
        # Stalled stage tracking
        self.stall_warnings = {}
    
    def update_counter(self, key: str, delta: int = 1, bytes_delta: int = 0):
        """Update a counter and activity timestamp."""
        if key.endswith('_frames'):
            self.counters[key] += delta
        if bytes_delta > 0:
            bytes_key = key.replace('_frames', '_bytes')
            if bytes_key in self.counters:
                self.counters[bytes_key] += bytes_delta
    
    def update_activity(self, stage: str):
        """Update last activity timestamp for a stage."""
        if stage in self.last_activity:
            self.last_activity[stage] = time.monotonic()
    
    def get_deltas(self):
        """Calculate deltas since last heartbeat."""
        deltas = {}
        for key, current in self.counters.items():
            prev = self.prev_counters.get(key, 0)
            deltas[key] = current - prev
        return deltas
    
    def format_heartbeat(self, queues: dict) -> str:
        """Format heartbeat line with deltas and queue sizes."""
        now = time.monotonic()
        elapsed = now - self.start_time
        deltas = self.get_deltas()
        
        # Queue sizes
        q8k = queues.get('pcm8k_q', 'N/A')
        q24k = queues.get('pcm24k_q', 'N/A')
        qogg = queues.get('ogg_q', 'N/A')
        qpcm = queues.get('pcm_out_q', 'N/A')
        
        # Format deltas
        delta_strs = []
        for key in ['exotel_in_bytes', 'resample_8k_to_24k_out_bytes', 'opus_encode_out_bytes',
                     'engine_out_bytes', 'engine_audio_bytes', 'decoder_out_pcm24k_bytes',
                     'exotel_out_bytes']:
            d = deltas.get(key, 0)
            if d > 0:
                delta_strs.append(f"Δ{key.split('_')[0]}={d:+d}B")
        
        # Last activity times (relative to now)
        last_strs = []
        for stage, last_time in self.last_activity.items():
            age = now - last_time
            if age < 10.0:  # Only show recent activity
                last_strs.append(f"{stage}={age:.1f}s")
        
        # Check for stalls
        stall_warnings = []
        for stage, last_time in self.last_activity.items():
            age = now - last_time
            if age > 3.0:
                # Check if upstream is moving
                upstream_moving = False
                if stage == 'encode_out':
                    upstream_moving = deltas.get('encode_in', 0) > 0
                elif stage == 'engine_out':
                    upstream_moving = deltas.get('encode_out', 0) > 0
                elif stage == 'decoder_out':
                    upstream_moving = deltas.get('engine_audio_bytes', 0) > 0
                elif stage == 'exotel_out':
                    upstream_moving = deltas.get('resample_24k_to_8k_out_bytes', 0) > 0
                
                if upstream_moving and stage not in self.stall_warnings:
                    self.stall_warnings[stage] = now
                    stall_warnings.append(f"STALLED_AT={stage}")
                elif not upstream_moving and stage in self.stall_warnings:
                    del self.stall_warnings[stage]
        
        # Build heartbeat line
        hb = f"HB t={elapsed:.1f}s q8k={q8k} q24k={q24k} qogg={qogg} qpcm={qpcm}"
        if delta_strs:
            hb += " " + " ".join(delta_strs)
        if last_strs:
            hb += " last: " + " ".join(last_strs[:5])  # Limit to 5 most recent
        if stall_warnings:
            hb += " " + " ".join(stall_warnings)
        
        # Update prev counters
        self.prev_counters = self.counters.copy()
        self.last_heartbeat_time = now
        
        return hb


# ========== HELPER FUNCTIONS ==========
def ssl_no_verify():
    """Create SSL context that doesn't verify certificates."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def int16_to_float32(pcm_bytes: bytes) -> np.ndarray:
    """Convert PCM16LE bytes to float32 array [-1, 1]."""
    x = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    return x / 32768.0


def float32_to_int16_bytes(x: np.ndarray) -> bytes:
    """Convert float32 array to PCM16LE bytes."""
    x = np.clip(x, -1.0, 1.0)
    y = (x * 32767.0).astype(np.int16)
    return y.tobytes()


# ========== TASK WRAPPER WITH EXCEPTION LOGGING ==========
async def run_task(name: str, coro):
    """Run async task with full lifecycle logging (start/exit/exception)."""
    logger.info(f"TASK_START: {name}")
    try:
        await coro
        logger.info(f"TASK_EXIT: {name} (normal)")
    except asyncio.CancelledError:
        logger.info(f"TASK_CANCEL: {name}")
        raise
    except Exception as e:
        logger.error(f"TASK_EXCEPTION: {name}: {e}", exc_info=True)
        raise


async def safe_task_wrapper(coro, task_name: str):
    """Legacy wrapper - use run_task instead."""
    return await run_task(task_name, coro)


# ========== MAIN BRIDGE HANDLER ==========
async def handler(exotel_ws, path=None):
    """Handle Exotel WebSocket connection and bridge to PersonaPlex."""
    client_addr = exotel_ws.remote_address if hasattr(exotel_ws, 'remote_address') else 'unknown'
    session_id = str(uuid.uuid4())[:8]
    logger.info(f"SESSION_START: {session_id} from {client_addr}")
    
    # Initialize observability
    obs = PipelineObservability(session_id)
    
    # Artifact capture setup
    capture_enabled = os.getenv("CAPTURE", "0") == "1"
    capture_dir = None
    capture_files = {}
    if capture_enabled:
        capture_dir = Path("/workspace/personaplex/captures") / session_id
        capture_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"CAPTURE: Writing artifacts to {capture_dir}")
        # Initialize capture files with size limits (2MB each)
        max_capture_size = 2 * 1024 * 1024
        capture_files = {
            'exotel_in': {'file': open(capture_dir / "exotel_in_pcm8k.raw", "wb"), 'size': 0, 'max': max_capture_size},
            'resampled_24k': {'file': open(capture_dir / "resampled_pcm24k.raw", "wb"), 'size': 0, 'max': max_capture_size},
            'encoder_out': {'file': open(capture_dir / "encoder_out.bin", "wb"), 'size': 0, 'max': max_capture_size},
            'engine_out': {'file': open(capture_dir / "engine_out.bin", "wb"), 'size': 0, 'max': max_capture_size},
            'decoder_out': {'file': open(capture_dir / "decoder_out_pcm24k.raw", "wb"), 'size': 0, 'max': max_capture_size},
            'exotel_out': {'file': open(capture_dir / "exotel_out_pcm8k.raw", "wb"), 'size': 0, 'max': max_capture_size},
        }
    
    # Connection state
    pp_ws = None
    resampler_8k_to_24k = None
    resampler_24k_to_8k = None
    opus_writer = None
    opus_reader = None
    tasks = []
    connection_active = True
    stop_received_ts = None  # Timestamp when STOP event received
    drain_mode = False  # True when in drain-after-STOP mode
    
    # Track timestamps for disconnect diagnosis
    last_exotel_inbound_ts = None
    last_exotel_outbound_ts = None
    last_engine_inbound_ts = None
    last_decoder_pcm_ts = None
    
    # Queues (defined once in handler scope - CREATE IMMEDIATELY)
    pcm8k_queue = asyncio.Queue(maxsize=500)  # Exotel PCM8k frames
    pcm24k_queue = asyncio.Queue(maxsize=500)  # Resampled PCM24k chunks
    opus_queue = asyncio.Queue(maxsize=500)  # Queue for Ogg Opus payloads
    pcm_out_queue = asyncio.Queue(maxsize=500)  # Queue for PCM8k chunks (20ms frames)
    
    # Log queue identity at creation
    logger.info(f"QUEUE_CREATE: pcm8k_queue id={id(pcm8k_queue)}")
    logger.info(f"QUEUE_CREATE: pcm24k_queue id={id(pcm24k_queue)}")
    logger.info(f"QUEUE_CREATE: opus_queue id={id(opus_queue)}")
    logger.info(f"QUEUE_CREATE: pcm_out_queue id={id(pcm_out_queue)}")
    
    try:
        # Connect to PersonaPlex with retry
        max_retries = 5
        retry_delay = 1.0
        
        for attempt in range(max_retries):
            try:
                logger.info(f"Connecting to PersonaPlex (attempt {attempt + 1}/{max_retries})")
                pp_ws = await websockets.connect(
                    PERSONAPLEX_WS,
                    ssl=ssl_no_verify(),
                    max_size=None,
                    ping_interval=5,
                    ping_timeout=5,
                    close_timeout=2,
                    open_timeout=10,
                )
                logger.info("Connected to PersonaPlex")
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(f"Connection attempt {attempt + 1} failed: {e}, retrying in {retry_delay}s...")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    logger.error(f"Failed to connect to PersonaPlex after {max_retries} attempts: {e}")
                    await exotel_ws.close()
                    return
        
        if pp_ws is None:
            logger.error("Could not establish PersonaPlex connection")
            import traceback
            logger.info(
                f"SERVER_INITIATED_EXOTEL_CLOSE: session={session_id} "
                f"reason=personaplex_ws_none stack={traceback.format_stack()[-2:-1]}"
            )
            await exotel_ws.close()
            return
        
        # Wait for handshake (0x00 byte)
        handshake_received = False
        for _ in range(150):  # 15 second timeout
            try:
                msg = await asyncio.wait_for(pp_ws.recv(), timeout=0.1)
                if isinstance(msg, (bytes, bytearray)) and len(msg) > 0:
                    frame_type = msg[0]
                    if frame_type == 0x00:
                        logger.info("Received PersonaPlex handshake (0x00)")
                        handshake_received = True
                        break
                    else:
                        logger.warning(f"Unexpected frame type {frame_type:02x} during handshake")
            except asyncio.TimeoutError:
                continue
        
        if not handshake_received:
            logger.error("Did not receive PersonaPlex handshake")
            import traceback
            logger.info(
                f"SERVER_INITIATED_EXOTEL_CLOSE: session={session_id} "
                f"reason=personaplex_handshake_timeout stack={traceback.format_stack()[-2:-1]}"
            )
            await exotel_ws.close()
            return
        
        # PHASE 2: Initialize Python-based resamplers (no FFmpeg subprocess buffering)
        resampler_8k_to_24k = PythonResampler(EXOTEL_SR, MODEL_SR, "8k->24k")
        resampler_24k_to_8k = PythonResampler(MODEL_SR, EXOTEL_SR, "24k->8k")
        
        try:
            resampler_8k_to_24k.start()
            resampler_24k_to_8k.start()
            logger.info("Python resamplers initialized (no subprocess)")
        except Exception as e:
            logger.error(f"Failed to start resamplers: {e}", exc_info=True)
            import traceback
            logger.info(
                f"SERVER_INITIATED_EXOTEL_CLOSE: session={session_id} "
                f"reason=resampler_start_failed stack={traceback.format_stack()[-2:-1]}"
            )
            await exotel_ws.close()
            return
        
        # PHASE B: Initialize FFmpeg Opus encoder for streaming encoding
        # Define FfmpegOpusEncoder class inline
        class FfmpegOpusEncoder:
            """Manages a persistent ffmpeg subprocess for PCM24k to raw Opus encoding (not Ogg).
            
            PHASE 2: Engine expects raw Opus packets (sphn.OpusStreamReader.append_bytes accepts ogg/opus bytes).
            We use raw Opus format (-f opus) instead of Ogg container for better streaming performance.
            """
            def __init__(self, sample_rate=24000, observability=None):
                self.sample_rate = sample_rate
                self.proc = None
                self._closed = False
                self.tag = "opus_encoder"
                self.stderr_buffer = bytearray()
                self.stderr_task = None
                self.encode_in_bytes_total = 0
                self.encode_out_bytes_total = 0
                self.encode_out_chunks_total = 0
                self.last_log_time = time.monotonic()
                self.observability = observability
            
            def start(self):
                """Start ffmpeg encoder subprocess for raw Opus packets."""
                try:
                    import shlex
                    # Use Ogg Opus format (engine expects Ogg based on logs showing OggS=True)
                    # Add -flush_packets 1 to force immediate output
                    command = [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel", "error",
                        "-f", "s16le",
                        "-ar", str(self.sample_rate),
                        "-ac", "1",
                        "-i", "pipe:0",
                        "-c:a", "libopus",
                        "-application", "voip",
                        "-frame_duration", "20",
                        "-vbr", "off",
                        "-b:a", "24k",
                        "-flush_packets", "1",  # Force immediate packet output
                        "-f", "ogg",  # Ogg Opus format (matches engine output)
                        "pipe:1"
                    ]
                    logger.info(f"Opus encoder cmd: {' '.join(shlex.quote(x) for x in command)}")
                    
                    self.proc = subprocess.Popen(
                        command,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        bufsize=0,
                    )
                    if self.proc.poll() is not None:
                        stderr = self.proc.stderr.read().decode(errors="ignore")
                        raise RuntimeError(f"{self.tag} exited immediately: {stderr}")
                    
                    logger.info(
                        f"{self.tag} started: PCM{self.sample_rate // 1000}k → Ogg Opus, "
                        f"PID={self.proc.pid}, stdin_open={self.proc.stdin is not None}, "
                        f"stdout_open={self.proc.stdout is not None}, stderr_open={self.proc.stderr is not None}"
                    )
                    
                    # STEP 3: Start async stderr reader task
                    self.stderr_task = asyncio.create_task(self._stderr_reader())
                    
                except FileNotFoundError:
                    raise RuntimeError(f"ffmpeg not found. Install with: apt-get install -y ffmpeg")
                except Exception as e:
                    raise RuntimeError(f"{self.tag} failed to start: {e}")
            
            async def _stderr_reader(self):
                """Continuously read and log stderr with prefix [ffmpeg-enc]. Uses non-blocking read1."""
                try:
                    loop = asyncio.get_event_loop()
                    line_buf = bytearray()
                    while not self._closed and self.proc and self.proc.poll() is None:
                        try:
                            # Use read1 (non-blocking) with small chunks
                            chunk = await asyncio.wait_for(
                                loop.run_in_executor(None, lambda: self.proc.stderr.read1(256)),
                                timeout=0.1
                            )
                            if chunk:
                                self.stderr_buffer.extend(chunk)
                                if len(self.stderr_buffer) > 1024:
                                    self.stderr_buffer = self.stderr_buffer[-512:]
                                
                                # Process complete lines
                                line_buf.extend(chunk)
                                while b'\n' in line_buf:
                                    line, line_buf = line_buf.split(b'\n', 1)
                                    if line:
                                        line_str = line.decode(errors="ignore").strip()
                                        if line_str:
                                            logger.warning(f"[ffmpeg-enc] {line_str}")
                            elif self.proc.poll() is not None:
                                break
                        except asyncio.TimeoutError:
                            continue
                        except Exception as e:
                            logger.error(f"[ffmpeg-enc] stderr reader error: {e}", exc_info=True)
                            break
                except Exception as e:
                    logger.error(f"[ffmpeg-enc] stderr reader fatal: {e}", exc_info=True)
            
            def write(self, pcm_bytes: bytes):
                """Write PCM bytes to encoder input with logging."""
                if self._closed or self.proc is None:
                    return False
                try:
                    self.proc.stdin.write(pcm_bytes)
                    self.proc.stdin.flush()
                    self.encode_in_bytes_total += len(pcm_bytes)
                    if self.observability:
                        self.observability.update_counter('opus_encode_in_pcm_bytes', bytes_delta=len(pcm_bytes))
                        self.observability.update_activity('encode_in')
                    
                    # Rate-limited logging (every 1 second)
                    now = time.monotonic()
                    if now - self.last_log_time >= 1.0:
                        logger.debug(
                            f"{self.tag} write: total_in={self.encode_in_bytes_total}b, "
                            f"total_out={self.encode_out_bytes_total}b, chunks={self.encode_out_chunks_total}"
                        )
                        self.last_log_time = now
                    
                    return True
                except BrokenPipeError:
                    logger.error(f"{self.tag} write: BrokenPipeError - process may have died")
                    self._closed = True
                    return False
                except OSError as e:
                    logger.error(f"{self.tag} write: OSError: {e}")
                    self._closed = True
                    return False
            
            async def read(self, nbytes: int, timeout: float = 0.1):
                """Read encoded Ogg Opus bytes (async) with metrics.
                
                Uses read1 (non-blocking) if available, else read with timeout.
                Returns empty bytes on timeout, None on error.
                """
                if self._closed or self.proc is None:
                    return None
                try:
                    loop = asyncio.get_event_loop()
                    # Try read1 first (non-blocking), fallback to read
                    if hasattr(self.proc.stdout, 'read1'):
                        data = await asyncio.wait_for(
                            loop.run_in_executor(None, self.proc.stdout.read1, min(nbytes, 4096)),
                            timeout=timeout
                        )
                    else:
                        data = await asyncio.wait_for(
                            loop.run_in_executor(None, self.proc.stdout.read, nbytes),
                            timeout=timeout
                        )
                    if data:
                        self.encode_out_bytes_total += len(data)
                        self.encode_out_chunks_total += 1
                        if self.observability:
                            self.observability.update_counter('opus_encode_out_bytes', bytes_delta=len(data))
                            self.observability.update_activity('encode_out')
                        
                        # Rate-limited logging (every 1 second)
                        now = time.monotonic()
                        if now - self.last_log_time >= 1.0:
                            logger.debug(
                                f"[ffmpeg-{self.tag}] read: got {len(data)} bytes, "
                                f"total_out={self.encode_out_bytes_total}b, chunks={self.encode_out_chunks_total}"
                            )
                            self.last_log_time = now
                    elif self.proc.poll() is not None:
                        logger.warning(f"[ffmpeg-{self.tag}] read: EOF and process exited (code={self.proc.returncode})")
                        self._closed = True
                    return data if data else b""
                except asyncio.TimeoutError:
                    return b""  # Timeout is normal, return empty bytes
                except Exception as e:
                    logger.error(f"[ffmpeg-{self.tag}] read error: {e}", exc_info=True)
                    self._closed = True
                    return None
            
            def check_stderr(self):
                """Check and return any stderr output from ffmpeg."""
                if self.proc and self.proc.stderr:
                    try:
                        err = self.proc.stderr.read()
                        if err:
                            self.stderr_buffer.extend(err)
                            if len(self.stderr_buffer) > 1024:
                                self.stderr_buffer = self.stderr_buffer[-512:]
                            return self.stderr_buffer.decode(errors="ignore")
                    except Exception:
                        pass
                return None
            
            def stop(self):
                """Stop and cleanup encoder."""
                self._closed = True
                
                # Cancel stderr reader task
                if self.stderr_task and not self.stderr_task.done():
                    self.stderr_task.cancel()
                
                if self.proc:
                    try:
                        self.proc.stdin.close()
                    except Exception:
                        pass
                    try:
                        self.proc.stdout.close()
                    except Exception:
                        pass
                    try:
                        self.proc.terminate()
                        self.proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                    except Exception:
                        pass
                    self.proc = None
                    logger.info(
                        f"{self.tag} stopped: total_in={self.encode_in_bytes_total}b, "
                        f"total_out={self.encode_out_bytes_total}b, chunks={self.encode_out_chunks_total}"
                    )
        
        # Initialize FFmpeg Opus encoder
        opus_encoder = None
        try:
            opus_encoder = FfmpegOpusEncoder(MODEL_SR, observability=obs)
            opus_encoder.start()
            logger.info("FFmpeg Opus encoder started")
        except Exception as e:
            logger.error(f"Failed to start FFmpeg Opus encoder: {e}", exc_info=True)
            await exotel_ws.close()
            return
        
        # Keep sphn as fallback (not used if FFmpeg works)
        opus_writer = None
        
        # Initialize FFmpeg Ogg decoder for inbound (Engine → Exotel)
        ogg_decoder = FfmpegOggDecoder(observability=obs)
        ogg_decoder.start()
        
        # Frame sizes
        # 20ms @ 24kHz = 480 samples = 960 bytes (PCM16LE)
        model_frame_samples = int(MODEL_SR * AUDIO_CHUNK_MS / 1000)
        model_frame_bytes = model_frame_samples * 2
        
        # Exotel: 20ms @ 8kHz = 160 samples = 320 bytes (smaller chunks for lower latency)
        exotel_frame_bytes = int(EXOTEL_SR * 0.02 * 2)  # 20ms chunks
        min_frame_bytes = max(160, exotel_frame_bytes // 2)  # At least 10ms @ 8kHz
        
        logger.info(f"Transcoding initialized: Exotel {EXOTEL_SR}Hz <-> Model {MODEL_SR}Hz")
        
        # Keepalive task - DISABLED: Engine rejects 0x00 keepalive messages
        # The engine handles keepalive internally via ping/pong
        async def personaplex_keepalive():
            # Just monitor connection, don't send keepalive
            while connection_active and pp_ws:
                await asyncio.sleep(5.0)
                # Connection health is maintained by ping_interval/ping_timeout
                if not connection_active:
                    break
        
        # Heartbeat task - logs pipeline status every 1 second
        async def heartbeat_task():
            """Log pipeline heartbeat with deltas and queue sizes."""
            logger.info(f"heartbeat_task: ENTRY queues: pcm8k={id(pcm8k_queue)}, pcm24k={id(pcm24k_queue)}, opus={id(opus_queue)}, pcm_out={id(pcm_out_queue)}")
            while connection_active:
                try:
                    await asyncio.sleep(1.0)
                    if not connection_active:
                        break
                    
                    # Access queues directly (they're in handler scope)
                    queues = {
                        'pcm8k_q': pcm8k_queue.qsize(),
                        'pcm24k_q': pcm24k_queue.qsize(),
                        'ogg_q': opus_queue.qsize(),
                        'pcm_out_q': pcm_out_queue.qsize(),
                    }
                    hb_line = obs.format_heartbeat(queues)
                    logger.info(hb_line)
                except Exception as e:
                    logger.exception(f"FATAL: heartbeat_task crashed: {e}")
                    raise
        
        # ========== INBOUND: Exotel -> Engine ==========
        # Three-stage streaming pipeline with queues (queues already created above)
        
        async def exotel_to_engine():
            """Stage 1 - Receive Exotel frames and push to queue."""
            # Verify queue identity
            logger.info(f"exotel_to_engine: ENTRY pcm8k_queue id={id(pcm8k_queue)}")
            logger.info("exotel_to_engine: started")
            last_audio_time = None
            SILENCE_TAIL_MS = 500  # 500ms silence to trigger engine response
            
            try:
                async for msg in exotel_ws:
                    if not connection_active:
                        break
                    
                    try:
                        data = json.loads(msg)
                    except json.JSONDecodeError:
                        logger.warning(f"Invalid JSON from Exotel: {msg[:100]}")
                        continue
                    
                    event_type = data.get("event")
                    
                    if event_type == "start":
                        logger.info("Exotel start event received")
                        obs.update_activity('exotel_in')
                        last_audio_time = time.monotonic()
                        continue
                    
                    if event_type == "stop":
                        stop_received_ts = time.monotonic()
                        stop_ts = stop_received_ts
                        ws_state = "open" if not exotel_ws.closed else "closed"
                        logger.info(
                            f"EXOTEL_STOP_EVENT: session={session_id} ts={stop_ts:.3f} "
                            f"ws_state={ws_state} exotel_in_frames={obs.counters.get('exotel_in_frames', 0)} "
                            f"exotel_out_frames={obs.counters.get('exotel_out_frames', 0)} "
                            f"engine_audio_frames={obs.counters.get('engine_audio_frames', 0)} "
                            f"decoder_pcm_bytes={obs.counters.get('decoder_out_pcm24k_bytes', 0)} "
                            f"drain_after_stop={EXOTEL_DRAIN_AFTER_STOP} "
                            f"next_action=break_exotel_to_engine_loop"
                        )
                        # Send silence tail
                        silence_pcm8k = np.zeros(int(EXOTEL_SR * SILENCE_TAIL_MS / 1000), dtype=np.int16)
                        silence_bytes = silence_pcm8k.tobytes()
                        try:
                            pcm8k_queue.put_nowait(silence_bytes)
                        except asyncio.QueueFull:
                            pass
                        # Signal end of input to pipeline
                        try:
                            await pcm8k_queue.put(None)
                        except Exception:
                            pass
                        # If drain mode enabled, set flag but don't break yet
                        if EXOTEL_DRAIN_AFTER_STOP:
                            drain_mode = True
                            logger.info(f"EXOTEL_DRAIN_MODE: Enabled, will drain for up to {EXOTEL_DRAIN_SECS}s")
                        break
                    
                    if event_type == "media":
                        payload = data.get("media", {}).get("payload")
                        if not payload:
                            logger.warning("Media event with no payload")
                            continue
                        
                        try:
                            pcm8k = base64.b64decode(payload)
                            obs.update_counter('exotel_in_frames', delta=1, bytes_delta=len(pcm8k))
                            obs.update_counter('pcm8k_in_bytes', bytes_delta=len(pcm8k))
                            obs.update_activity('exotel_in')
                            obs.update_activity('pcm8k')
                            last_audio_time = time.monotonic()
                            
                            # PHASE 1: Live debugging - Exotel WS receive
                            print(f"[LIVE][EXOTEL_IN] bytes={len(pcm8k)} q_pcm8k={pcm8k_queue.qsize()} t={time.time()}")
                            logger.info(f"exotel_to_engine: received media frame {obs.counters['exotel_in_frames']}: {len(pcm8k)} bytes")
                            last_exotel_inbound_ts = time.monotonic()
                            
                            # Artifact capture
                            if capture_enabled and 'exotel_in' in capture_files:
                                cap = capture_files['exotel_in']
                                if cap['size'] < cap['max']:
                                    cap['file'].write(pcm8k)
                                    cap['size'] += len(pcm8k)
                            
                            if obs.counters['exotel_in_frames'] <= 5:
                                logger.info(f"exotel_to_engine: received media frame {obs.counters['exotel_in_frames']}: {len(pcm8k)} bytes")
                            
                            # Push to queue (non-blocking)
                            try:
                                pcm8k_queue.put_nowait(pcm8k)
                                # PHASE 1: Live debugging - pcm8k_queue.put()
                                print(f"[LIVE][PCM8K_PUT] bytes={len(pcm8k)} q_pcm8k={pcm8k_queue.qsize()} t={time.time()}")
                                if obs.counters['exotel_in_frames'] <= 5:
                                    logger.info(f"exotel_to_engine: put {len(pcm8k)} bytes into pcm8k_queue (qsize={pcm8k_queue.qsize()})")
                            except asyncio.QueueFull:
                                # Drop oldest
                                try:
                                    pcm8k_queue.get_nowait()
                                    pcm8k_queue.put_nowait(pcm8k)
                                    logger.warning("pcm8k_queue full, dropped oldest")
                                except asyncio.QueueEmpty:
                                    pass
                        except Exception as e:
                            logger.exception(f"FATAL: Error processing media frame: {e}")
                            raise
                
                # After Exotel closes, send silence tail
                if last_audio_time:
                    logger.info("Sending silence tail to trigger engine response")
                    silence_pcm8k = np.zeros(int(EXOTEL_SR * SILENCE_TAIL_MS / 1000), dtype=np.int16)
                    silence_bytes = silence_pcm8k.tobytes()
                    try:
                        pcm8k_queue.put_nowait(silence_bytes)
                    except asyncio.QueueFull:
                        pass
                    # Signal end of input
                    await pcm8k_queue.put(None)
                else:
                    await pcm8k_queue.put(None)
                    
            except websockets.exceptions.ConnectionClosed:
                logger.info("Exotel connection closed (normal)")
            except Exception as e:
                logger.exception(f"FATAL: exotel_to_engine crashed: {e}")
                raise
            finally:
                logger.info(f"exotel_to_engine: exiting, sent {obs.counters['engine_out_frames']} frames, {obs.counters['engine_out_bytes']} bytes to engine")
        
        # Stage 2 - Resampler loop (8k → 24k)
        async def resample_loop():
            """Continuously resample PCM8k to PCM24k."""
            # Verify queue identity
            logger.info(f"resample_loop: ENTRY pcm8k_queue id={id(pcm8k_queue)}, pcm24k_queue id={id(pcm24k_queue)}")
            last_progress_ts = time.monotonic()
            frames_processed = 0
            
            try:
                while connection_active:
                    try:
                        # Watchdog: if queue has items but no progress for 2s, abort
                        if pcm8k_queue.qsize() > 0:
                            if time.monotonic() - last_progress_ts > 2.0:
                                logger.error(f"PIPELINE STALL DETECTED at resample_loop: queue_size={pcm8k_queue.qsize()}, no progress for {time.monotonic() - last_progress_ts:.1f}s")
                                raise RuntimeError("resample_loop stalled")
                        
                        pcm8k = await asyncio.wait_for(pcm8k_queue.get(), timeout=1.0)
                        
                        # Log first get
                        if frames_processed == 0:
                            logger.info(f"resample_loop: FIRST_GET pcm8k_queue id={id(pcm8k_queue)}, got {len(pcm8k) if pcm8k else 0} bytes")
                        
                        if pcm8k is None:  # End signal
                            logger.info("resample_loop: received None signal, exiting")
                            break
                        
                        assert pcm8k, "Got empty PCM8k frame"
                        assert len(pcm8k) > 0, f"Got zero-length PCM8k frame"
                        
                        logger.debug(f"resample_loop: got {len(pcm8k)} bytes")
                        frames_processed += 1
                        last_progress_ts = time.monotonic()
                        
                        obs.update_counter('resample_8k_to_24k_in_bytes', bytes_delta=len(pcm8k))
                        obs.update_activity('resample_in')
                        
                        # PHASE 1: Live debugging - resample_loop before resampler
                        print(f"[LIVE][RESAMPLE][IN] bytes={len(pcm8k)} q_pcm8k={pcm8k_queue.qsize()} q_pcm24k={pcm24k_queue.qsize()} t={time.time()}")
                        
                        # Use Python resampler (synchronous, no subprocess)
                        pcm24k = resampler_8k_to_24k.resample(pcm8k)
                        
                        # PHASE 1: Live debugging - resample_loop after resampler
                        print(f"[LIVE][RESAMPLE][OUT] bytes={len(pcm24k)} q_pcm8k={pcm8k_queue.qsize()} q_pcm24k={pcm24k_queue.qsize()} t={time.time()}")
                        
                        if not pcm24k or len(pcm24k) == 0:
                            logger.error(f"FATAL: Python resampler returned empty output for {len(pcm8k)} bytes input")
                            raise RuntimeError("Resampler returned empty output")
                        
                        logger.debug(f"resample_loop: produced {len(pcm24k)} bytes")
                        obs.update_counter('resample_8k_to_24k_out_bytes', bytes_delta=len(pcm24k))
                        obs.update_activity('resample_out')
                        
                        # Artifact capture
                        if capture_enabled and 'resampled_24k' in capture_files:
                            cap = capture_files['resampled_24k']
                            if cap['size'] < cap['max']:
                                cap['file'].write(pcm24k)
                                cap['size'] += len(pcm24k)
                        
                        # Push to next stage
                        try:
                            await pcm24k_queue.put(pcm24k)
                            # PHASE 1: Live debugging - pcm24k_queue.put()
                            print(f"[LIVE][PCM24K_PUT] bytes={len(pcm24k)} q_pcm24k={pcm24k_queue.qsize()} t={time.time()}")
                            if frames_processed <= 5:
                                logger.info(f"resample_loop: put {len(pcm24k)} bytes into pcm24k_queue (qsize={pcm24k_queue.qsize()})")
                        except asyncio.QueueFull:
                            try:
                                pcm24k_queue.get_nowait()
                                await pcm24k_queue.put(pcm24k)
                                logger.warning("pcm24k_queue full, dropped oldest")
                            except asyncio.QueueEmpty:
                                pass
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        # PHASE 2: Live debugging - explicit exception handling
                        print(f"[LIVE][ERROR][RESAMPLE] {repr(e)} t={time.time()}")
                        logger.exception(f"FATAL: resample_loop crashed: {e}")
                        raise
                
                # Signal end to encoder
                await pcm24k_queue.put(None)
                logger.info(f"resample_loop: processed {frames_processed} frames, exiting normally")
            except Exception as e:
                logger.exception(f"FATAL: resample_loop fatal error: {e}")
                raise
            finally:
                logger.info(f"resample_loop: exiting, processed={frames_processed}, resample_out={obs.counters['resample_8k_to_24k_out_bytes']}b")
                resampler_8k_to_24k.stop()
        
        # Stage 3 - Encoder loop (PCM24k → Ogg Opus → engine)
        async def encode_and_send_loop():
            """Continuously encode PCM24k to Ogg Opus and send to engine.
            
            Buffers PCM24k to accumulate 200ms (9600 bytes @ 24kHz) before writing to FFmpeg,
            then aggressively drains encoder output to minimize latency.
            """
            # Verify queue identity
            logger.info(f"encode_and_send_loop: ENTRY pcm24k_queue id={id(pcm24k_queue)}")
            engine_send_frames = 0
            engine_send_bytes = 0
            last_progress_ts = time.monotonic()
            frames_processed = 0
            encode_batch_writes = 0
            encode_batch_bytes = 0
            encode_out_chunks = 0
            first_output_sent = False
            first_output_log_time = 0
            
            # PCM buffering: accumulate 200ms @ 24kHz = 9600 bytes before writing
            THRESHOLD_MS = 200
            THRESHOLD_BYTES = int(MODEL_SR * (THRESHOLD_MS / 1000) * 2)  # 24kHz * 0.2s * 2 bytes/sample
            MAX_BUF_BYTES = int(MODEL_SR * 2.0 * 2)  # Cap at 2 seconds
            pcm_buf = bytearray()
            
            logger.info(f"encode_and_send_loop: PCM buffer threshold={THRESHOLD_BYTES}b ({THRESHOLD_MS}ms), max={MAX_BUF_BYTES}b")
            
            async def drain_encoder_output(is_first_drain=False):
                """Aggressively drain encoder stdout after a write. Uses non-blocking reads."""
                nonlocal engine_send_frames, engine_send_bytes, encode_out_chunks, first_output_sent, first_output_log_time
                
                # For first drain, use longer timeout (Ogg header/page may take longer)
                base_timeout = 1.0 if is_first_drain else 0.2
                max_drain_attempts = 10
                empty_reads = 0
                total_drained = 0
                chunks_read = 0
                
                if is_first_drain:
                    logger.info("Waiting for first Ogg page from encoder...")
                
                for attempt in range(max_drain_attempts):
                    # Check encoder health
                    if opus_encoder.proc and opus_encoder.proc.poll() is not None:
                        logger.error(f"FATAL: Opus encoder process died (code={opus_encoder.proc.returncode})")
                        stderr_output = opus_encoder.check_stderr()
                        if stderr_output:
                            logger.error(f"[ffmpeg-enc] stderr: {stderr_output}")
                        raise RuntimeError("Opus encoder process died")
                    
                    # Non-blocking read with timeout
                    opus_chunk = await opus_encoder.read(4096, timeout=base_timeout)
                    
                    # PHASE 1: Live debugging - encode_loop after encoder.read()
                    if opus_chunk:
                        print(f"[LIVE][ENCODE][OUT] bytes={len(opus_chunk)} t={time.time()}")
                    
                    if not opus_chunk:
                        empty_reads += 1
                        # Stop after 2 consecutive empty reads
                        if empty_reads >= 2:
                            break
                        continue
                    
                    empty_reads = 0
                    chunks_read += 1
                    total_drained += len(opus_chunk)
                    encode_out_chunks += 1
                    
                    # Send to engine
                    try:
                        frame_data = b"\x01" + opus_chunk
                        # PHASE 1: Live debugging - engine_ws.send()
                        print(f"[LIVE][ENGINE_SEND] bytes={len(opus_chunk)} t={time.time()}")
                        await pp_ws.send(frame_data)
                        engine_send_frames += 1
                        engine_send_bytes += len(opus_chunk)
                        obs.update_counter('engine_out_frames', delta=1, bytes_delta=len(opus_chunk))
                        obs.update_activity('engine_out')
                        
                        # Log first success with requested format
                        if not first_output_sent:
                            first_output_sent = True
                            first_output_log_time = time.monotonic()
                            logger.info(f"ENCODER_EMIT_OK: {len(opus_chunk)} bytes, ENGINE_SEND_OK frame#{engine_send_frames}")
                        elif time.monotonic() - first_output_log_time >= 1.0:
                            # Rate-limited: once per second after first
                            logger.info(f"ENCODER_EMIT: got {len(opus_chunk)} bytes; ENGINE_SEND: frame#{engine_send_frames} (total_out={opus_encoder.encode_out_bytes_total}b)")
                            first_output_log_time = time.monotonic()
                        
                        if engine_send_frames <= 10:
                            logger.info(f"✓ Sent engine frame {engine_send_frames}: {len(opus_chunk)} bytes (Ogg Opus)")
                        
                        # Artifact capture
                        if capture_enabled and 'encoder_out' in capture_files:
                            cap = capture_files['encoder_out']
                            if cap['size'] < cap['max']:
                                cap['file'].write(frame_data)
                                cap['size'] += len(frame_data)
                    except Exception as e:
                        logger.exception(f"FATAL: Failed to send to engine: {e}")
                        raise
                    
                    # Continue reading if we got a full chunk (might be more)
                    if len(opus_chunk) < 4096:
                        break  # Partial chunk, likely no more data for now
                
                if chunks_read > 0:
                    logger.debug(f"drain_encoder_output: read {chunks_read} chunks, {total_drained} bytes")
                elif is_first_drain:
                    logger.warning(f"drain_encoder_output: first drain produced no output (encoder_in={opus_encoder.encode_in_bytes_total}b)")
                
                return chunks_read > 0
            
            first_batch_write_ts = None
            
            try:
                while connection_active:
                    try:
                        # Explicit watchdog: if pcm24k_queue is non-empty or pcm_buf grows, but encode_out_bytes stays 0 for >2 seconds after first batch write
                        queue_size = pcm24k_queue.qsize()
                        buf_size = len(pcm_buf)
                        encode_out_bytes = opus_encoder.encode_out_bytes_total if opus_encoder else 0
                        
                        if first_batch_write_ts is not None and encode_out_bytes == 0:
                            stall_duration = time.monotonic() - first_batch_write_ts
                            if stall_duration > 2.0 and (queue_size > 0 or buf_size > 0):
                                logger.error(
                                    f"PIPELINE STALL DETECTED at encode_and_send_loop: "
                                    f"queue_size={queue_size}, buf_size={buf_size}, encode_out_bytes=0 for {stall_duration:.1f}s after first batch write, "
                                    f"encoder_in={opus_encoder.encode_in_bytes_total if opus_encoder else 0}b"
                                )
                                raise RuntimeError("Encoder stalled/buffering")
                        
                        # General progress watchdog
                        if (queue_size > 0 or buf_size > 0) and time.monotonic() - last_progress_ts > 2.0:
                            logger.error(
                                f"PIPELINE STALL DETECTED at encode_and_send_loop: "
                                f"queue_size={queue_size}, buf_size={buf_size}, no progress for {time.monotonic() - last_progress_ts:.1f}s, "
                                f"encoder_in={opus_encoder.encode_in_bytes_total if opus_encoder else 0}b, "
                                f"encoder_out={encode_out_bytes}b"
                            )
                            raise RuntimeError("encode_and_send_loop stalled: encoder buffering/no output")
                        
                        pcm24k = await asyncio.wait_for(pcm24k_queue.get(), timeout=1.0)
                        
                        # Log first get
                        if frames_processed == 0:
                            logger.info(f"encode_and_send_loop: FIRST_GET pcm24k_queue id={id(pcm24k_queue)}, got {len(pcm24k) if pcm24k else 0} bytes")
                        
                        if pcm24k is None:  # End signal
                            # Flush any remaining buffer
                            if len(pcm_buf) > 0:
                                logger.info(f"encode_and_send_loop: flushing final buffer ({len(pcm_buf)} bytes)")
                                if opus_encoder.write(bytes(pcm_buf)):
                                    encode_batch_writes += 1
                                    encode_batch_bytes += len(pcm_buf)
                                    await drain_encoder_output(is_first_drain=(encode_batch_writes == 1))
                                pcm_buf.clear()
                            
                            # Close stdin and drain remaining output
                            if opus_encoder and opus_encoder.proc:
                                try:
                                    opus_encoder.proc.stdin.close()
                                except Exception:
                                    pass
                            # Final drain
                            for _ in range(10):
                                opus_chunk = await opus_encoder.read(4096, timeout=0.1)
                                if not opus_chunk:
                                    break
                                try:
                                    frame_data = b"\x01" + opus_chunk
                                    await pp_ws.send(frame_data)
                                    engine_send_frames += 1
                                    engine_send_bytes += len(opus_chunk)
                                    obs.update_counter('engine_out_frames', delta=1, bytes_delta=len(opus_chunk))
                                    obs.update_activity('engine_out')
                                    
                                    if capture_enabled and 'encoder_out' in capture_files:
                                        cap = capture_files['encoder_out']
                                        if cap['size'] < cap['max']:
                                            cap['file'].write(frame_data)
                                            cap['size'] += len(frame_data)
                                except Exception as e:
                                    logger.exception(f"FATAL: Failed to send final frame: {e}")
                                    raise
                            break
                        
                        assert pcm24k, "Got empty PCM24k frame"
                        assert len(pcm24k) > 0, f"Got zero-length PCM24k frame"
                        
                        frames_processed += 1
                        obs.update_counter('pcm24k_buffer_bytes', bytes_delta=len(pcm24k))
                        
                        # Accumulate into buffer
                        pcm_buf.extend(pcm24k)
                        
                        # Check buffer cap (safety)
                        if len(pcm_buf) > MAX_BUF_BYTES:
                            logger.error(f"FATAL: PCM buffer exceeded max ({len(pcm_buf)} > {MAX_BUF_BYTES} bytes), flushing anyway")
                        
                        # Write to encoder when threshold reached
                        if len(pcm_buf) >= THRESHOLD_BYTES:
                            batch = bytes(pcm_buf)
                            pcm_buf.clear()
                            
                            # PHASE 1: Live debugging - encode_loop before encoder.write()
                            print(f"[LIVE][ENCODE][IN] bytes={len(batch)} q_pcm24k={pcm24k_queue.qsize()} t={time.time()}")
                            
                            if not opus_encoder.write(batch):
                                logger.error(f"FATAL: Opus encoder write failed after {frames_processed} frames")
                                raise RuntimeError("Opus encoder write failed")
                            
                            encode_batch_writes += 1
                            encode_batch_bytes += len(batch)
                            last_progress_ts = time.monotonic()  # Update on write
                            
                            # Track first batch write timestamp for explicit watchdog
                            if encode_batch_writes == 1:
                                first_batch_write_ts = time.monotonic()
                            
                            if encode_batch_writes <= 5:
                                logger.info(f"encode_loop: wrote batch #{encode_batch_writes} ({len(batch)} bytes, {len(batch)/MODEL_SR/2*1000:.1f}ms) to encoder (total_in={opus_encoder.encode_in_bytes_total}b)")
                            
                            # Aggressively drain encoder output
                            is_first_drain = (encode_batch_writes == 1)
                            output_produced = await drain_encoder_output(is_first_drain=is_first_drain)
                            
                            if output_produced:
                                last_progress_ts = time.monotonic()  # Update on successful read/send
                                first_batch_write_ts = None  # Reset watchdog once we get output
                            
                            if encode_batch_writes <= 3 and not output_produced:
                                logger.warning(
                                    f"encode_loop: batch #{encode_batch_writes} produced no output "
                                    f"(encoder_in={opus_encoder.encode_in_bytes_total}b encoder_out={opus_encoder.encode_out_bytes_total}b)"
                                )
                    except asyncio.TimeoutError:
                        # Timeout waiting for queue - try to drain any pending encoder output
                        if opus_encoder and opus_encoder.proc and opus_encoder.proc.poll() is None:
                            # Try a quick read
                            opus_chunk = await opus_encoder.read(4096, timeout=0.05)
                            if opus_chunk:
                                try:
                                    frame_data = b"\x01" + opus_chunk
                                    await pp_ws.send(frame_data)
                                    engine_send_frames += 1
                                    engine_send_bytes += len(opus_chunk)
                                    last_progress_ts = time.monotonic()
                                    obs.update_counter('engine_out_frames', delta=1, bytes_delta=len(opus_chunk))
                                    obs.update_activity('engine_out')
                                    
                                    if capture_enabled and 'encoder_out' in capture_files:
                                        cap = capture_files['encoder_out']
                                        if cap['size'] < cap['max']:
                                            cap['file'].write(frame_data)
                                            cap['size'] += len(frame_data)
                                except Exception as e:
                                    logger.exception(f"FATAL: Failed to send to engine: {e}")
                                    raise
                        continue
                    except Exception as e:
                        # PHASE 2: Live debugging - explicit exception handling
                        print(f"[LIVE][ERROR][ENCODE] {repr(e)} t={time.time()}")
                        logger.exception(f"FATAL: encode_and_send_loop crashed: {e}")
                        raise
            except Exception as e:
                logger.exception(f"FATAL: encode_and_send_loop fatal error: {e}")
                raise
            finally:
                logger.info(f"ENGINE_SEND frames={engine_send_frames} bytes={engine_send_bytes}")
                logger.info(
                    f"encode_and_send_loop: exiting, processed={frames_processed} frames, "
                    f"batches={encode_batch_writes} ({encode_batch_bytes}b), "
                    f"encode_out={opus_encoder.encode_out_bytes_total if opus_encoder else 0}b, "
                    f"chunks={encode_out_chunks}"
                )
        
        # ========== OUTBOUND: Engine -> Exotel ==========
        # Three-stage pipeline: receive → decode → send
        # Queues already created above
        async def engine_to_exotel():
            """Receive engine binary frames, decode Ogg Opus to PCM24k via FFmpeg, resample to 8k, send to Exotel."""
            nonlocal last_engine_inbound_ts, last_decoder_pcm_ts, last_exotel_outbound_ts, stop_received_ts, drain_mode
            frame_log_count = 0
            last_exotel_out_time = time.time()
            chunk_size_8k = 320  # 20ms @ 8kHz = 160 samples * 2 bytes = 320 bytes
            
            # STAGE 1: Engine receive loop - instrumented with detailed logging
            async def engine_recv_loop():
                nonlocal frame_log_count
                last_frame_time = time.time()
                session_start = time.time()
                try:
                    while connection_active and pp_ws:
                        try:
                            # PHASE 1: Longer timeout, better error handling
                            msg = await asyncio.wait_for(pp_ws.recv(), timeout=5.0)
                            # PHASE 1: Live debugging - engine_ws.recv()
                            if isinstance(msg, (bytes, bytearray)) and len(msg) > 0:
                                print(f"[LIVE][ENGINE_RECV] bytes={len(msg)} t={time.time()}")
                                last_engine_inbound_ts = time.monotonic()
                            last_frame_time = time.time()
                        except asyncio.TimeoutError:
                            # PHASE 1: Session watchdog
                            elapsed = time.monotonic() - session_start
                            time_since_frame = time.monotonic() - last_frame_time
                            if int(elapsed) % 5 == 0 and elapsed > 0:  # Every 5 seconds
                                logger.info(
                                    f"Session watchdog: elapsed={elapsed:.1f}s, "
                                    f"engine_audio={obs.counters['engine_audio_frames']}f/{obs.counters['engine_audio_bytes']}b, "
                                    f"time_since_frame={time_since_frame:.1f}s"
                                )
                            continue
                        except websockets.exceptions.ConnectionClosedOK as e:
                            logger.warning(
                                f"Engine WS closed OK: code={e.code}, reason={e.reason}, "
                                f"frames_received={obs.counters['engine_audio_frames']}"
                            )
                            break
                        except websockets.exceptions.ConnectionClosedError as e:
                            logger.error(
                                f"Engine WS closed ERROR: code={e.code}, reason={e.reason}, "
                                f"frames_received={obs.counters['engine_audio_frames']}",
                                exc_info=True
                            )
                            break
                        except asyncio.CancelledError:
                            logger.warning("engine_recv_loop cancelled")
                            raise
                        except Exception as e:
                            # PHASE 2: Live debugging - explicit exception handling
                            print(f"[LIVE][ERROR][ENGINE_RECV] {repr(e)} t={time.time()}")
                            logger.error(f"Error receiving from engine: {e}", exc_info=True)
                            raise
                        
                        if not isinstance(msg, (bytes, bytearray)) or len(msg) == 0:
                            continue
                        
                        frame_type = msg[0]
                        payload = msg[1:] if len(msg) > 1 else b""
                        
                        obs.update_counter('engine_in_frames', delta=1, bytes_delta=len(msg))
                        obs.update_activity('engine_in')
                        
                        # Instrument first 50 frames
                        if frame_log_count < 50:
                            is_ogg = payload.startswith(b"OggS") if len(payload) >= 4 else False
                            has_opushead = b"OpusHead" in payload[:200] if len(payload) >= 8 else False
                            logger.info(
                                f"ENGINE_FRAME #{frame_log_count+1}: type=0x{frame_type:02x}, "
                                f"payload_len={len(payload)}, OggS={is_ogg}, OpusHead={has_opushead}"
                            )
                            frame_log_count += 1
                        
                        # 0x00: handshake/keepalive (ignore, don't process)
                        if frame_type == 0x00:
                            continue
                        
                        # 0x02: text tokens
                        if frame_type == 0x02:
                            obs.update_counter('engine_text_frames', delta=1)
                            try:
                                text = payload.decode("utf-8", errors="ignore")
                                if frame_log_count <= 10:
                                    logger.info(f"Engine text: {text[:100]}")
                            except Exception:
                                pass
                            continue
                        
                        # 0x01: audio Ogg Opus payload - push to queue
                        if frame_type == 0x01:
                            obs.update_counter('engine_audio_frames', delta=1, bytes_delta=len(payload))
                            
                            # Artifact capture
                            if capture_enabled and 'engine_out' in capture_files:
                                cap = capture_files['engine_out']
                                if cap['size'] < cap['max']:
                                    cap['file'].write(payload)
                                    cap['size'] += len(payload)
                            
                            # Non-blocking queue put to prevent backpressure
                            try:
                                opus_queue.put_nowait(payload)
                            except asyncio.QueueFull:
                                # Drop oldest if queue full
                                try:
                                    opus_queue.get_nowait()
                                    opus_queue.put_nowait(payload)
                                    logger.warning("Opus queue full, dropped oldest packet")
                                except asyncio.QueueEmpty:
                                    pass
                except Exception as e:
                    logger.error(f"engine_recv_loop error: {e}", exc_info=True)
                finally:
                    # Signal decode loop to stop
                    try:
                        await opus_queue.put(None)
                    except Exception:
                        pass
            
            # STAGE 2: FFmpeg decode - split into feed and read loops
            async def ffmpeg_feed_loop():
                """Feed Ogg packets to FFmpeg continuously."""
                total_ogg_bytes = 0
                iterations = 0
                try:
                    while connection_active:
                        # Drain opus_queue and feed to FFmpeg
                        ogg_buffer = bytearray()
                        packets_this_batch = 0
                        
                        # Collect a batch of Ogg packets
                        while packets_this_batch < 20:
                            try:
                                payload = await asyncio.wait_for(opus_queue.get(), timeout=0.1)
                                if payload is None:  # Sentinel to stop
                                    # Close stdin to signal end of stream (allows FFmpeg to flush)
                                    if ogg_decoder.proc and ogg_decoder.proc.stdin:
                                        try:
                                            ogg_decoder.proc.stdin.close()
                                            logger.info("FFmpeg decoder stdin closed, allowing flush of remaining data")
                                        except Exception:
                                            pass
                                    # Don't return immediately - let read loop drain remaining data
                                    # The read loop will detect stdin closed and continue until process exits
                                    break
                                ogg_buffer.extend(payload)
                                packets_this_batch += 1
                            except asyncio.TimeoutError:
                                break

                        if len(ogg_buffer) == 0:
                            await asyncio.sleep(0.01)
                            continue
                        
                        total_ogg_bytes += len(ogg_buffer)
                        iterations += 1
                        
                        # Write Ogg bytes to FFmpeg decoder
                        if not ogg_decoder.write(bytes(ogg_buffer)):
                            logger.error("FFmpeg decoder write failed")
                            break
                        
                        if iterations <= 5:
                            logger.info(f"FFmpeg feed: wrote {len(ogg_buffer)} bytes Ogg (total: {total_ogg_bytes})")
                except Exception as e:
                    # PHASE 2: Live debugging - explicit exception handling
                    print(f"[LIVE][ERROR][FFMPEG_FEED] {repr(e)} t={time.time()}")
                    logger.error(f"ffmpeg_feed_loop error: {e}", exc_info=True)
                    raise
            
            async def ffmpeg_read_loop():
                """Continuously read PCM24k from FFmpeg and process."""
                total_pcm24k_bytes = 0
                iterations = 0
                try:
                    while connection_active:
                        # Continuously read from FFmpeg (non-blocking with timeout)
                        # Use longer timeout to allow FFmpeg to flush buffered data
                        pcm24k = await ogg_decoder.read(8192, timeout=1.0)
                        
                        if pcm24k:
                            last_decoder_pcm_ts = time.monotonic()
                        
                        if not pcm24k:
                            # Check if decoder stdin is closed (feed loop done) and process still alive
                            # If so, continue reading to drain remaining buffered data
                            if ogg_decoder.proc and ogg_decoder.proc.stdin and ogg_decoder.proc.stdin.closed:
                                if ogg_decoder.proc.poll() is not None:
                                    # Process exited, no more data
                                    logger.info("FFmpeg decoder process exited, read loop done")
                                    break
                                # Process still alive but stdin closed - may have buffered data
                                # Continue reading with shorter sleep
                                await asyncio.sleep(0.01)
                                continue
                            await asyncio.sleep(0.01)
                            continue
                        
                        total_pcm24k_bytes += len(pcm24k)
                        iterations += 1
                        
                        # PHASE 1: Live debugging - decode_loop after decoder.read()
                        print(f"[LIVE][DECODE][OUT] bytes={len(pcm24k)} t={time.time()}")
                        
                        if iterations <= 5:
                            logger.info(f"FFmpeg read: got {len(pcm24k)} bytes PCM24k (total: {total_pcm24k_bytes})")
                        
                        # Use Python resampler for 24k->8k (no FFmpeg subprocess)
                        obs.update_counter('resample_24k_to_8k_in_bytes', bytes_delta=len(pcm24k))
                        obs.update_activity('resample_down_in')
                        pcm8k = resampler_24k_to_8k.resample(pcm24k)
                        if pcm8k:
                            obs.update_counter('resample_24k_to_8k_out_bytes', bytes_delta=len(pcm8k))
                            obs.update_activity('resample_down_out')
                            
                            # Artifact capture
                            if capture_enabled and 'decoder_out' in capture_files:
                                cap = capture_files['decoder_out']
                                if cap['size'] < cap['max']:
                                    cap['file'].write(pcm24k)
                                    cap['size'] += len(pcm24k)
                            
                            # Chunk into 20ms frames and put in queue
                            offset = 0
                            while offset + chunk_size_8k <= len(pcm8k):
                                chunk = pcm8k[offset:offset+chunk_size_8k]
                                try:
                                    await pcm_out_queue.put(chunk)
                                except asyncio.QueueFull:
                                    logger.warning("PCM8k queue full, dropping chunk")
                                offset += chunk_size_8k
                            
                            # Handle remainder
                            if offset < len(pcm8k):
                                remainder = pcm8k[offset:]
                                if len(remainder) >= 160:  # At least 10ms
                                    try:
                                        await pcm_out_queue.put(remainder)
                                    except asyncio.QueueFull:
                                        pass
                except Exception as e:
                    # PHASE 2: Live debugging - explicit exception handling
                    print(f"[LIVE][ERROR][FFMPEG_READ] {repr(e)} t={time.time()}")
                    logger.error(f"ffmpeg_read_loop error: {e}", exc_info=True)
                    raise
            
            # STAGE 3: Exotel send loop - drain pcm8k_queue and send JSON frames
            async def exotel_send_loop():
                nonlocal last_exotel_out_time, drain_mode, stop_received_ts
                exotel_send_frames = 0
                exotel_send_bytes = 0
                last_silence_send_ts = None
                try:
                    while connection_active or (drain_mode and stop_received_ts is not None):
                        # In drain mode, check exit conditions
                        if drain_mode and stop_received_ts is not None:
                            now = time.monotonic()
                            elapsed = now - stop_received_ts
                            if elapsed >= EXOTEL_DRAIN_SECS:
                                logger.info(f"EXOTEL_DRAIN_MODE: Timeout ({EXOTEL_DRAIN_SECS}s) reached, exiting")
                                drain_mode = False
                                break
                            # Check if playback finished (queue empty AND no new decoder audio for 500ms)
                            if pcm_out_queue.qsize() == 0:
                                time_since_decoder = now - last_decoder_pcm_ts if last_decoder_pcm_ts else float('inf')
                                if time_since_decoder >= 0.5:
                                    logger.info(f"EXOTEL_DRAIN_MODE: Playback finished (no decoder audio for {time_since_decoder:.2f}s), exiting")
                                    drain_mode = False
                                    break
                        
                        try:
                            timeout = 0.2 if (drain_mode and EXOTEL_SEND_SILENCE_WHEN_IDLE) else 1.0
                            pcm8k_chunk = await asyncio.wait_for(pcm_out_queue.get(), timeout=timeout)
                        except asyncio.TimeoutError:
                            # In drain mode with silence keepalive, send silence frames
                            if drain_mode and EXOTEL_SEND_SILENCE_WHEN_IDLE and not exotel_ws.closed:
                                now = time.monotonic()
                                if last_silence_send_ts is None or (now - last_silence_send_ts) >= 0.02:  # 20ms cadence
                                    # Send valid silence frame (320 bytes @ 8kHz PCM)
                                    silence_pcm8k = np.zeros(160, dtype=np.int16)  # 20ms @ 8kHz = 160 samples
                                    silence_bytes = silence_pcm8k.tobytes()
                                    payload_b64 = base64.b64encode(silence_bytes).decode("ascii")
                                    media_frame = {"event": "media", "media": {"payload": payload_b64}}
                                    try:
                                        await exotel_ws.send(json.dumps(media_frame))
                                        last_silence_send_ts = now
                                        if int(now * 10) % 50 == 0:  # Log every 5s
                                            logger.debug(f"EXOTEL_DRAIN_MODE: Sent silence keepalive frame")
                                    except Exception:
                                        pass  # Socket may be closed, will be caught below
                                continue
                            
                            # Check for latency (non-drain mode)
                            now = time.monotonic()
                            if (now - last_exotel_out_time) > 3.0 and obs.counters['engine_audio_frames'] > 0 and obs.counters['exotel_out_frames'] == 0:
                                logger.warning(
                                    f"LATENCY: No exotel_out for {now - last_exotel_out_time:.1f}s "
                                    f"while engine_audio={obs.counters['engine_audio_frames']}f/{obs.counters['engine_audio_bytes']}b"
                                )
                            continue
                        
                        # Encode to base64 and send JSON
                        payload_b64 = base64.b64encode(pcm8k_chunk).decode("ascii")
                        media_frame = {
                            "event": "media",
                            "media": {"payload": payload_b64}
                        }
                        
                        try:
                            # Check if Exotel websocket is closed before sending
                            if exotel_ws.closed:
                                logger.warning("EXOTEL_SEND: Exotel websocket closed, cannot send")
                                break
                            
                            # PHASE 1: Live debugging - exotel_ws.send()
                            print(f"[LIVE][EXOTEL_OUT] bytes={len(pcm8k_chunk)} q_pcm_out={pcm_out_queue.qsize()} t={time.time()}")
                            await exotel_ws.send(json.dumps(media_frame))
                            exotel_send_frames += 1
                            exotel_send_bytes += len(pcm8k_chunk)
                            last_exotel_outbound_ts = time.monotonic()
                            obs.update_counter('exotel_out_frames', delta=1, bytes_delta=len(pcm8k_chunk))
                            obs.update_activity('exotel_out')
                            last_exotel_out_time = time.monotonic()
                            
                            # Artifact capture
                            if capture_enabled and 'exotel_out' in capture_files:
                                cap = capture_files['exotel_out']
                                if cap['size'] < cap['max']:
                                    cap['file'].write(pcm8k_chunk)
                                    cap['size'] += len(pcm8k_chunk)
                            
                            if exotel_send_frames <= 10:
                                logger.info(f"✓ Sent frame {exotel_send_frames} to Exotel: {len(pcm8k_chunk)} bytes")
                        except Exception as e:
                            # PHASE 2: Live debugging - explicit exception handling
                            print(f"[LIVE][ERROR][EXOTEL_SEND] {repr(e)} t={time.time()}")
                            logger.error(f"Failed to send to Exotel: {e}", exc_info=True)
                            raise
                except Exception as e:
                    # PHASE 2: Live debugging - explicit exception handling
                    print(f"[LIVE][ERROR][EXOTEL_SEND_LOOP] {repr(e)} t={time.time()}")
                    logger.error(f"exotel_send_loop error: {e}", exc_info=True)
                    raise
                finally:
                    logger.info(f"EXOTEL_SEND frames={exotel_send_frames} bytes={exotel_send_bytes}")
            
            # Start all stages (4 tasks: recv, feed, read, send)
            logger.info("Starting engine_to_exotel tasks: engine_recv, ffmpeg_feed, ffmpeg_read, exotel_send")
            try:
                await asyncio.gather(
                    safe_task_wrapper(engine_recv_loop(), "engine_recv"),
                    safe_task_wrapper(ffmpeg_feed_loop(), "ffmpeg_feed"),
                    safe_task_wrapper(ffmpeg_read_loop(), "ffmpeg_read"),
                    safe_task_wrapper(exotel_send_loop(), "exotel_send")
                )
            except Exception as e:
                logger.error(f"engine_to_exotel gather error: {e}", exc_info=True)
            finally:
                logger.info(
                    f"engine_to_exotel: engine_audio={obs.counters['engine_audio_frames']}f/{obs.counters['engine_audio_bytes']}b, "
                    f"exotel_out={obs.counters['exotel_out_frames']}f/{obs.counters['exotel_out_bytes']}b"
                )
                logger.info(f"DECODER_PCM bytes={obs.counters['decoder_out_pcm24k_bytes']}")
        
        # PHASE 1: Start all pipeline tasks with explicit logging
        tasks = []
        try:
            logger.info("PHASE 1: Creating pipeline tasks...")
            task_exotel_recv = asyncio.create_task(safe_task_wrapper(exotel_to_engine(), "exotel_to_engine"))
            logger.info("START task: exotel_to_engine")
            
            task_resample = asyncio.create_task(safe_task_wrapper(resample_loop(), "resample_loop"))
            logger.info("START task: resample_loop")
            
            task_encode = asyncio.create_task(safe_task_wrapper(encode_and_send_loop(), "encode_and_send_loop"))
            logger.info("START task: encode_and_send_loop")
            
            task_engine_recv = asyncio.create_task(safe_task_wrapper(engine_to_exotel(), "engine_to_exotel"))
            logger.info("START task: engine_to_exotel")
            
            task_keepalive = asyncio.create_task(run_task("keepalive", personaplex_keepalive()))
            logger.info("START task: personaplex_keepalive")
            
            task_heartbeat = asyncio.create_task(run_task("heartbeat", heartbeat_task()))
            logger.info("START task: heartbeat")

            tasks = [
                task_keepalive,
                task_heartbeat,
                task_exotel_recv,
                task_resample,
                task_encode,
                task_engine_recv,
            ]
            logger.info(f"PHASE 1: All {len(tasks)} tasks created and started")
        except Exception as e:
            logger.error(f"Failed to create tasks: {e}", exc_info=True)
            tasks = []
        
        # PHASE 2: Wait for Exotel client to close, NOT for any task to complete
        # The engine connection should stay open as long as Exotel client is connected
        # OR during drain-after-STOP mode
        try:
            # Monitor Exotel client connection
            while connection_active or (drain_mode and stop_received_ts is not None):
                try:
                    # Check if Exotel client is still connected
                    # In drain mode, use shorter timeout to check drain conditions
                    timeout = 0.5 if drain_mode else 1.0
                    await asyncio.wait_for(exotel_ws.wait_closed(), timeout=timeout)
                    # Exotel closed the websocket - log with full context
                    close_code = getattr(exotel_ws, 'close_code', None)
                    close_reason = getattr(exotel_ws, 'close_reason', '')
                    is_normal = close_code in (1000, 1001) if close_code else False
                    logger.info(
                        f"EXOTEL_CLIENT_DISCONNECTED: session={session_id} "
                        f"close_code={close_code} close_reason={close_reason} is_normal={is_normal} "
                        f"last_exotel_inbound={last_exotel_inbound_ts} "
                        f"last_exotel_outbound={last_exotel_outbound_ts} "
                        f"last_engine_inbound={last_engine_inbound_ts} "
                        f"last_decoder_pcm={last_decoder_pcm_ts} "
                        f"exotel_in_frames={obs.counters.get('exotel_in_frames', 0)} "
                        f"exotel_out_frames={obs.counters.get('exotel_out_frames', 0)} "
                        f"engine_audio_frames={obs.counters.get('engine_audio_frames', 0)} "
                        f"decoder_pcm_bytes={obs.counters.get('decoder_out_pcm24k_bytes', 0)}"
                    )
                    logger.info("Exotel client disconnected")
                    # In drain mode, socket close is expected - continue draining if enabled
                    if drain_mode and EXOTEL_DRAIN_AFTER_STOP:
                        logger.info("EXOTEL_DRAIN_MODE: Socket closed but drain mode active, continuing...")
                        # Don't break - let drain mode exit naturally
                    else:
                        break
                except asyncio.TimeoutError:
                    # Client still connected (or in drain mode), check if any critical task died
                    # In drain mode, also check drain exit conditions
                    if drain_mode and stop_received_ts is not None:
                        now = time.monotonic()
                        elapsed = now - stop_received_ts
                        if elapsed >= EXOTEL_DRAIN_SECS:
                            logger.info(f"EXOTEL_DRAIN_MODE: Timeout reached, ending drain")
                            drain_mode = False
                            connection_active = False
                            break
                        # Check if playback finished
                        if pcm_out_queue.qsize() == 0:
                            time_since_decoder = now - last_decoder_pcm_ts if last_decoder_pcm_ts else float('inf')
                            if time_since_decoder >= 0.5:
                                logger.info(f"EXOTEL_DRAIN_MODE: Playback finished, ending drain")
                                drain_mode = False
                                connection_active = False
                                break
                    
                    # Client still connected, check if any critical task died
                    for task in tasks:
                        if task.done():
                            try:
                                task.result()  # Raise exception if task failed
                            except Exception as e:
                                logger.error(f"Critical task failed: {e}", exc_info=True)
                                connection_active = False
                                break
                    if not connection_active:
                        break
                    continue
                except Exception as e:
                    logger.error(f"Error monitoring Exotel connection: {e}", exc_info=True)
                    break
        except Exception as e:
            logger.error(f"Task monitoring error: {e}", exc_info=True)
        
        logger.info("Connection closing, cleaning up tasks...")
        connection_active = False
        
        # PHASE 3: Cancel remaining tasks
        for task in tasks:
            if not task.done():
                logger.debug(f"Cancelling task: {task}")
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except asyncio.TimeoutError:
                    logger.warning(f"Task {task} did not cancel within timeout")
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.error(f"Task cancellation error: {e}", exc_info=True)
    
    except Exception as e:
        logger.error(f"Handler error: {e}", exc_info=True)
    finally:
        # Cleanup
        connection_active = False
        
        # Cancel and await all tasks safely
        # Note: 'tasks' is defined in the handler scope, so it should be accessible here
        try:
            if tasks:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                # Wait for all tasks to complete (with exceptions ignored)
                await asyncio.gather(*tasks, return_exceptions=True)
        except NameError:
            # tasks not defined (shouldn't happen, but be safe)
            logger.warning("Tasks list not found in cleanup")
        except Exception as e:
            logger.error(f"Error during task cleanup: {e}", exc_info=True)
        
        if pp_ws:
            try:
                await pp_ws.close()
            except Exception:
                pass

        if resampler_8k_to_24k:
            resampler_8k_to_24k.stop()
        
        if resampler_24k_to_8k:
            resampler_24k_to_8k.stop()
        
        if ogg_decoder:
            ogg_decoder.stop()
        
        # Close capture files
        if capture_enabled and capture_files:
            for name, cap_info in capture_files.items():
                try:
                    if cap_info['file']:
                        cap_info['file'].close()
                        logger.info(f"CAPTURE: Closed {name}, size={cap_info['size']}B")
                except Exception as e:
                    logger.error(f"Error closing capture file {name}: {e}")
        
        logger.info(f"SESSION_END: {session_id} cleanup complete")


async def main():
    """Main entry point."""
    logger.info("=" * 60)
    logger.info("Exotel Bridge Starting")
    logger.info("=" * 60)
    logger.info(f"Bridge listening on: ws://{BRIDGE_HOST}:{BRIDGE_PORT}")
    logger.info(f"PersonaPlex engine: {PERSONAPLEX_WS}")
    logger.info(f"Audio: Exotel {EXOTEL_SR}Hz <-> Model {MODEL_SR}Hz")
    logger.info(f"Voice prompt: {VOICE_PROMPT}")
    logger.info(f"Text prompt: {TEXT_PROMPT[:50]}...")
    logger.info("=" * 60)
    logger.info("Bridge ready! Waiting for Exotel connections...")
    logger.info("")
    
    async with websockets.serve(handler, BRIDGE_HOST, BRIDGE_PORT, max_size=None):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
