#!/usr/bin/env python3
"""
Deterministic test: Generate 24kHz PCM, encode via FFmpeg (RAW/OGG), send to engine.
Strict timeouts, non-blocking IO, always exits in <= 15 seconds.
"""
import asyncio
import numpy as np
import ssl
import subprocess
import sys
import time
from urllib.parse import quote

try:
    import websockets
except ImportError:
    print("ERROR: websockets not installed")
    sys.exit(1)


SAMPLE_RATE = 24000
CHUNK_MS = 20
CHUNK_BYTES = (SAMPLE_RATE * CHUNK_MS // 1000) * 2  # 960 bytes
PCM_DURATION_SEC = 3
MAX_TEST_TIME = 15.0
ENGINE_URL = "wss://127.0.0.1:8998/api/chat"
VOICE_PROMPT = "NATF0.pt"
TEXT_PROMPT = "You enjoy having a good conversation."


def generate_pcm(duration_sec, sample_rate=24000):
    """Generate 3 seconds of 440Hz sine wave PCM16LE."""
    num_samples = int(duration_sec * sample_rate)
    t = np.linspace(0, duration_sec, num_samples, False)
    signal = np.sin(2 * np.pi * 440 * t) * 0.8
    pcm_samples = (signal * 32767).astype(np.int16)
    return pcm_samples.tobytes()


async def test_mode(mode, pcm_data):
    """Test one encoder mode with strict timeouts."""
    mode_name = "RAW OPUS" if mode == "raw" else "OGG OPUS"
    print(f"\n{'='*60}")
    print(f"Testing {mode_name}")
    print(f"{'='*60}")
    
    # Build FFmpeg command
    if mode == "raw":
        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
            "-c:a", "libopus", "-application", "voip", "-frame_duration", "20",
            "-vbr", "off", "-b:a", "24k", "-f", "opus", "pipe:1"
        ]
    else:
        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
            "-c:a", "libopus", "-application", "voip", "-frame_duration", "20",
            "-vbr", "off", "-b:a", "24k", "-f", "ogg", "pipe:1"
        ]
    
    results = {
        "mode": mode,
        "encoder_in_bytes": 0,
        "encoder_out_bytes": 0,
        "engine_out_frames": 0,
        "engine_recv_audio_frames": 0,
        "engine_recv_text_frames": 0,
        "engine_close_code": None,
        "engine_close_reason": None,
        "error": None
    }
    
    proc = None
    ws = None
    
    try:
        # Start FFmpeg
        proc = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0
        )
        
        if proc.poll() is not None:
            stderr = proc.stderr.read(1024).decode(errors="ignore")
            results["error"] = f"FFmpeg exited: {stderr}"
            return results
        
        # Connect to engine
        engine_url = f"{ENGINE_URL}?voice_prompt={quote(VOICE_PROMPT)}&text_prompt={quote(TEXT_PROMPT)}"
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        
        print(f"Connecting to engine...")
        ws = await asyncio.wait_for(
            websockets.connect(engine_url, ssl=ssl_ctx),
            timeout=5.0
        )
        print("✓ Connected")
        
        # Read handshake with timeout (non-blocking, don't fail if timeout)
        handshake_received = False
        try:
            handshake = await asyncio.wait_for(ws.recv(), timeout=3.0)
            if isinstance(handshake, bytes):
                print(f"✓ Handshake: {handshake[:16].hex()}...")
                handshake_received = True
            else:
                print(f"✓ Handshake: {handshake}")
                handshake_received = True
        except asyncio.TimeoutError:
            print("⚠ Handshake timeout (continuing anyway)")
        
        # Feed PCM to encoder: accumulate 200ms (9600 bytes) then write, then continue
        async def feed_pcm():
            try:
                total = 0
                # Accumulate first 200ms before writing (like bridge does)
                THRESHOLD_BYTES = 9600  # 200ms @ 24kHz
                pcm_buf = bytearray()
                feed_duration = 2.0
                feed_end = time.time() + feed_duration
                pos = 0
                first_write_done = False
                
                while time.time() < feed_end and pos < len(pcm_data):
                    chunk = pcm_data[pos:pos + CHUNK_BYTES]
                    if not chunk:
                        break
                    
                    pcm_buf.extend(chunk)
                    pos += len(chunk)
                    
                    # Write when threshold reached
                    if len(pcm_buf) >= THRESHOLD_BYTES or (time.time() >= feed_end - 0.1 and len(pcm_buf) > 0):
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(
                            None, 
                            lambda: proc.stdin.write(bytes(pcm_buf)) or proc.stdin.flush()
                        )
                        total += len(pcm_buf)
                        results["encoder_in_bytes"] += len(pcm_buf)
                        if not first_write_done:
                            print(f"✓ First write: {len(pcm_buf)} bytes (threshold reached)")
                            first_write_done = True
                        pcm_buf.clear()
                    
                    await asyncio.sleep(CHUNK_MS / 1000.0)
                
                # Write any remaining
                if pcm_buf:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(
                        None,
                        lambda: proc.stdin.write(bytes(pcm_buf)) or proc.stdin.flush()
                    )
                    total += len(pcm_buf)
                    results["encoder_in_bytes"] += len(pcm_buf)
                
                # Close stdin to flush
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, proc.stdin.close)
                print(f"✓ Fed {total} bytes total, stdin closed")
            except Exception as e:
                results["error"] = f"feed_pcm: {e}"
                import traceback
                traceback.print_exc()
        
        # Drain encoder stdout using read1 pattern (like bridge)
        async def drain_and_send():
            try:
                drain_start = time.time()
                drain_end = time.time() + 5.0
                loop = asyncio.get_event_loop()
                empty_reads = 0
                is_first_drain = True
                
                while time.time() < drain_end:
                    # Use read1 if available (non-blocking), else read with timeout
                    timeout = 1.0 if is_first_drain else 0.2
                    try:
                        if hasattr(proc.stdout, 'read1'):
                            chunk = await asyncio.wait_for(
                                loop.run_in_executor(None, proc.stdout.read1, 4096),
                                timeout=timeout
                            )
                        else:
                            chunk = await asyncio.wait_for(
                                loop.run_in_executor(None, proc.stdout.read, 4096),
                                timeout=timeout
                            )
                    except asyncio.TimeoutError:
                        empty_reads += 1
                        if proc.poll() is not None and empty_reads >= 2:
                            break
                        if is_first_drain and empty_reads >= 5:
                            print(f"  ⚠ First drain: no output after {empty_reads * timeout:.1f}s")
                        continue
                    
                    if chunk:
                        empty_reads = 0
                        is_first_drain = False
                        results["encoder_out_bytes"] += len(chunk)
                        frame = b"\x01" + chunk
                        await ws.send(frame)
                        results["engine_out_frames"] += 1
                        elapsed = time.time() - drain_start
                        print(f"  [{elapsed:.2f}s] Sent frame {results['engine_out_frames']}: {len(chunk)} bytes")
                    elif proc.poll() is not None:
                        # Process done, try final read
                        try:
                            final = await asyncio.wait_for(
                                loop.run_in_executor(None, proc.stdout.read, 4096),
                                timeout=0.5
                            )
                            if final:
                                results["encoder_out_bytes"] += len(final)
                                frame = b"\x01" + final
                                await ws.send(frame)
                                results["engine_out_frames"] += 1
                                print(f"  Final chunk: {len(final)} bytes")
                        except:
                            pass
                        break
                    else:
                        empty_reads += 1
                        if empty_reads >= 10:
                            break
                
                if results["encoder_out_bytes"] == 0:
                    print(f"  ⚠ No encoder output after {time.time() - drain_start:.2f}s")
            except Exception as e:
                results["error"] = f"drain_and_send: {e}"
                import traceback
                traceback.print_exc()
        
        # Read engine responses (also handles handshake if not received yet)
        async def read_engine():
            try:
                read_end = time.time() + 5.0
                first_msg = True
                while time.time() < read_end:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        if isinstance(msg, bytes) and len(msg) > 0:
                            frame_type = msg[0]
                            if first_msg and frame_type == 0x00:
                                print(f"✓ Handshake received: {len(msg)} bytes")
                                first_msg = False
                                continue
                            first_msg = False
                            
                            if frame_type == 0x01:
                                results["engine_recv_audio_frames"] += 1
                                if results["engine_recv_audio_frames"] <= 5:
                                    print(f"  Audio frame {results['engine_recv_audio_frames']}: {len(msg)} bytes")
                            elif frame_type == 0x02:
                                results["engine_recv_text_frames"] += 1
                                text = msg[1:].decode('utf-8', errors='ignore')[:50]
                                print(f"  Text frame {results['engine_recv_text_frames']}: {text}")
                            elif frame_type == 0x00:
                                print(f"  Keepalive: {len(msg)} bytes")
                    except asyncio.TimeoutError:
                        continue
            except Exception as e:
                if "error" not in results or not results["error"]:
                    results["error"] = f"read_engine: {e}"
                import traceback
                traceback.print_exc()
        
        # Run all tasks
        start_time = time.time()
        await asyncio.gather(
            feed_pcm(),
            drain_and_send(),
            read_engine(),
            return_exceptions=True
        )
        
        # Read stderr for diagnostics
        try:
            stderr_data = proc.stderr.read(4096)
            if stderr_data:
                stderr_text = stderr_data.decode(errors="ignore")
                if stderr_text.strip():
                    print(f"  FFmpeg stderr: {stderr_text[:200]}")
        except:
            pass
        
        # Cleanup
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutError:
                proc.kill()
        
        if ws:
            try:
                results["engine_close_code"] = ws.close_code
                results["engine_close_reason"] = ws.close_reason
            except:
                pass
            await ws.close()
        
        elapsed = time.time() - start_time
        print(f"✓ Test completed in {elapsed:.2f}s")
        
    except Exception as e:
        results["error"] = str(e)
        import traceback
        traceback.print_exc()
    finally:
        if proc and proc.poll() is None:
            proc.kill()
        if ws:
            try:
                await ws.close()
            except:
                pass
    
    return results


async def main():
    """Main with hard timeout."""
    print("="*60)
    print("Deterministic Engine Test with Generated PCM")
    print("="*60)
    
    # Generate PCM
    print(f"\nGenerating {PCM_DURATION_SEC}s of 24kHz PCM...")
    pcm_data = generate_pcm(PCM_DURATION_SEC, SAMPLE_RATE)
    print(f"✓ Generated {len(pcm_data)} bytes")
    
    # Test both modes
    results_raw = await test_mode("raw", pcm_data)
    await asyncio.sleep(1)
    results_ogg = await test_mode("ogg", pcm_data)
    
    # Summary
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    
    for res in [results_raw, results_ogg]:
        mode_name = "RAW OPUS" if res["mode"] == "raw" else "OGG OPUS"
        print(f"\n{mode_name}:")
        print(f"  encoder_in_bytes: {res['encoder_in_bytes']}")
        print(f"  encoder_out_bytes: {res['encoder_out_bytes']}")
        print(f"  engine_out_frames: {res['engine_out_frames']}")
        print(f"  engine_recv_audio_frames: {res['engine_recv_audio_frames']}")
        print(f"  engine_recv_text_frames: {res['engine_recv_text_frames']}")
        if res["error"]:
            print(f"  ERROR: {res['error']}")
    
    # Verdict
    print("\n" + "="*60)
    print("VERDICT")
    print("="*60)
    
    raw_works = (results_raw["encoder_out_bytes"] > 0 and 
                 results_raw["engine_recv_audio_frames"] >= 10)
    ogg_works = (results_ogg["encoder_out_bytes"] > 0 and 
                 results_ogg["engine_recv_audio_frames"] >= 10)
    
    if raw_works:
        print("✓ RAW OPUS WORKS - Engine accepts raw opus streaming")
        print("  → Bridge should use -f opus (raw opus)")
        sys.exit(0)
    elif ogg_works:
        print("✓ OGG OPUS WORKS - Engine accepts ogg opus")
        print("  → Bridge needs OGG buffering fix")
        sys.exit(0)
    elif results_raw["encoder_out_bytes"] > 0 or results_ogg["encoder_out_bytes"] > 0:
        print("✗ Encoder produces output but engine responds with <10 frames")
        print("  → Engine framing issue or format mismatch")
        sys.exit(1)
    else:
        print("✗ Encoder produces no output")
        print("  → FFmpeg stdout drain issue")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(asyncio.wait_for(main(), timeout=MAX_TEST_TIME))
    except asyncio.TimeoutError:
        print("\n✗ Test exceeded maximum time limit")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n✗ Interrupted")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ FATAL: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
