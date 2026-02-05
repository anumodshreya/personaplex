#!/usr/bin/env python3
"""
Test PersonaPlex engine with synthetic 24kHz PCM audio.
Tests both RAW OPUS and OGG OPUS encoder modes to determine which works.
"""
import asyncio
import numpy as np
import ssl
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

try:
    import websockets
except ImportError:
    print("ERROR: websockets not installed. Run: pip install websockets")
    sys.exit(1)


# Configuration
ENGINE_URL = "wss://127.0.0.1:8998/api/chat"
VOICE_PROMPT = "NATF0.pt"
TEXT_PROMPT = "You enjoy having a good conversation."
SAMPLE_RATE = 24000
CHUNK_MS = 20
CHUNK_BYTES = (SAMPLE_RATE * CHUNK_MS // 1000) * 2  # 960 bytes for 20ms @ 24kHz
TEST_DURATION_SEC = 5
FEED_DURATION_SEC = 3  # Feed audio for 3 seconds


def generate_pcm_audio(duration_sec=5, sample_rate=24000, signal_type="sine"):
    """
    Generate mono PCM16LE audio.
    
    Args:
        duration_sec: Duration in seconds
        sample_rate: Sample rate (Hz)
        signal_type: "sine" (440 Hz) or "noise" (white noise)
    
    Returns:
        bytes: PCM16LE audio data
    """
    num_samples = int(duration_sec * sample_rate)
    
    if signal_type == "sine":
        # 440 Hz sine wave
        t = np.linspace(0, duration_sec, num_samples, False)
        signal = np.sin(2 * np.pi * 440 * t)
    elif signal_type == "noise":
        # White noise
        signal = np.random.randn(num_samples)
    else:
        raise ValueError(f"Unknown signal_type: {signal_type}")
    
    # Normalize to int16 range
    signal = signal / np.max(np.abs(signal)) * 0.8  # 80% volume
    pcm_samples = (signal * 32767).astype(np.int16)
    
    # Convert to little-endian bytes
    pcm_bytes = pcm_samples.tobytes()
    
    return pcm_bytes


async def test_encoder_mode(mode, pcm_data):
    """
    Test one encoder mode (RAW OPUS or OGG OPUS).
    
    Args:
        mode: "raw" or "ogg"
        pcm_data: PCM16LE bytes
    
    Returns:
        dict: Test results
    """
    print(f"\n{'=' * 60}")
    print(f"Testing MODE {mode.upper()}: {'RAW OPUS' if mode == 'raw' else 'OGG OPUS'}")
    print(f"{'=' * 60}")
    
    # Build FFmpeg command
    if mode == "raw":
        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-f", "s16le",
            "-ar", str(SAMPLE_RATE),
            "-ac", "1",
            "-i", "pipe:0",
            "-c:a", "libopus",
            "-application", "voip",
            "-frame_duration", "20",
            "-vbr", "off",
            "-b:a", "24k",
            "-f", "opus",
            "pipe:1"
        ]
    else:  # ogg
        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-f", "s16le",
            "-ar", str(SAMPLE_RATE),
            "-ac", "1",
            "-i", "pipe:0",
            "-c:a", "libopus",
            "-application", "voip",
            "-frame_duration", "20",
            "-vbr", "off",
            "-b:a", "24k",
            "-f", "ogg",
            "pipe:1"
        ]
    
    print(f"FFmpeg command: {' '.join(ffmpeg_cmd)}")
    
    # Build engine URL
    engine_url = f"{ENGINE_URL}?voice_prompt={quote(VOICE_PROMPT)}&text_prompt={quote(TEXT_PROMPT)}"
    
    # SSL context
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    
    results = {
        "mode": mode,
        "encoder_out_bytes": 0,
        "engine_sent_frames": 0,
        "engine_recv_audio_frames": 0,
        "engine_recv_text_frames": 0,
        "engine_close_code": None,
        "engine_close_reason": None,
        "ffmpeg_stderr": "",
        "error": None
    }
    
    try:
        # Start FFmpeg process
        proc = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0
        )
        
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode(errors="ignore")
            results["error"] = f"FFmpeg exited immediately: {stderr}"
            results["ffmpeg_stderr"] = stderr
            return results
        
        # Connect to engine
        print(f"Connecting to engine: {engine_url}")
        ws = await websockets.connect(engine_url, ssl=ssl_ctx, open_timeout=10)
        print("✓ Connected to engine")
        
        # Read handshake
        try:
            handshake = await asyncio.wait_for(ws.recv(), timeout=5.0)
            if isinstance(handshake, bytes) and len(handshake) > 0:
                print(f"✓ Received handshake: {handshake.hex()[:32]}...")
            else:
                print(f"✓ Received handshake: {handshake}")
        except asyncio.TimeoutError:
            print("⚠ Timeout waiting for handshake")
        
        # Start reading FFmpeg stdout and engine responses in parallel
        async def feed_audio():
            """Feed PCM data to FFmpeg in chunks."""
            try:
                total_fed = 0
                feed_end_time = time.time() + FEED_DURATION_SEC
                chunk_size = CHUNK_BYTES
                
                while time.time() < feed_end_time and total_fed < len(pcm_data):
                    chunk = pcm_data[total_fed:total_fed + chunk_size]
                    if not chunk:
                        break
                    
                    proc.stdin.write(chunk)
                    proc.stdin.flush()
                    total_fed += len(chunk)
                    
                    await asyncio.sleep(CHUNK_MS / 1000.0)  # Wait for next chunk
                
                print(f"✓ Fed {total_fed} bytes of PCM to encoder")
                proc.stdin.close()
            except Exception as e:
                print(f"ERROR in feed_audio: {e}")
                results["error"] = f"feed_audio error: {e}"
        
        async def drain_encoder_and_send():
            """Drain FFmpeg stdout and send to engine."""
            try:
                while True:
                    # Non-blocking read
                    chunk = proc.stdout.read(4096)
                    if chunk:
                        results["encoder_out_bytes"] += len(chunk)
                        # Send to engine with 0x01 prefix
                        frame = b"\x01" + chunk
                        await ws.send(frame)
                        results["engine_sent_frames"] += 1
                        print(f"  Sent frame {results['engine_sent_frames']}: {len(chunk)} bytes")
                    elif proc.poll() is not None:
                        # Process finished
                        break
                    else:
                        # No data yet, wait a bit
                        await asyncio.sleep(0.01)
            except Exception as e:
                print(f"ERROR in drain_encoder_and_send: {e}")
                results["error"] = f"drain_encoder_and_send error: {e}"
        
        async def read_engine_responses():
            """Read responses from engine."""
            try:
                read_end_time = time.time() + TEST_DURATION_SEC
                while time.time() < read_end_time:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        if isinstance(msg, bytes) and len(msg) > 0:
                            frame_type = msg[0]
                            if frame_type == 0x01:
                                results["engine_recv_audio_frames"] += 1
                                print(f"  Received audio frame {results['engine_recv_audio_frames']}: {len(msg)} bytes")
                            elif frame_type == 0x02:
                                results["engine_recv_text_frames"] += 1
                                text = msg[1:].decode('utf-8', errors='ignore')
                                print(f"  Received text frame {results['engine_recv_text_frames']}: {text[:50]}")
                            elif frame_type == 0x00:
                                print(f"  Received handshake/keepalive: {len(msg)} bytes")
                            else:
                                print(f"  Received unknown frame type 0x{frame_type:02x}: {len(msg)} bytes")
                    except asyncio.TimeoutError:
                        # Continue waiting
                        continue
            except Exception as e:
                print(f"ERROR in read_engine_responses: {e}")
                results["error"] = f"read_engine_responses error: {e}"
        
        # Run all tasks in parallel
        start_time = time.time()
        await asyncio.gather(
            feed_audio(),
            drain_encoder_and_send(),
            read_engine_responses(),
            return_exceptions=True
        )
        
        # Get FFmpeg stderr
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutError:
            proc.kill()
            proc.wait()
        
        stderr_output = proc.stderr.read().decode(errors="ignore")
        if stderr_output:
            results["ffmpeg_stderr"] = stderr_output
            print(f"\nFFmpeg stderr:\n{stderr_output}")
        
        # Get close code/reason
        try:
            results["engine_close_code"] = ws.close_code
            results["engine_close_reason"] = ws.close_reason
        except:
            pass
        
        await ws.close()
        print(f"✓ Test completed in {time.time() - start_time:.2f} seconds")
        
    except Exception as e:
        print(f"ERROR in test_encoder_mode: {e}")
        import traceback
        traceback.print_exc()
        results["error"] = str(e)
    
    return results


async def main():
    """Main test function."""
    print("=" * 60)
    print("PersonaPlex Engine Test with Synthetic PCM Audio")
    print("=" * 60)
    
    # Generate PCM audio
    print(f"\nGenerating {TEST_DURATION_SEC} seconds of 24kHz PCM audio (sine wave)...")
    pcm_data = generate_pcm_audio(duration_sec=TEST_DURATION_SEC, sample_rate=SAMPLE_RATE, signal_type="sine")
    print(f"✓ Generated {len(pcm_data)} bytes of PCM16LE audio")
    print(f"  Expected: {TEST_DURATION_SEC * SAMPLE_RATE * 2} bytes")
    
    # Create artifacts directory
    artifacts_dir = Path("/workspace/personaplex/artifacts")
    artifacts_dir.mkdir(exist_ok=True)
    
    # Save PCM for inspection
    pcm_path = artifacts_dir / "generated_pcm_24k.raw"
    with open(pcm_path, "wb") as f:
        f.write(pcm_data)
    print(f"✓ Saved PCM to: {pcm_path}")
    
    # Test both modes
    all_results = []
    
    # MODE A: RAW OPUS
    results_raw = await test_encoder_mode("raw", pcm_data)
    all_results.append(results_raw)
    
    # Wait a bit between tests
    await asyncio.sleep(2)
    
    # MODE B: OGG OPUS
    results_ogg = await test_encoder_mode("ogg", pcm_data)
    all_results.append(results_ogg)
    
    # Print summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    for res in all_results:
        mode_name = "RAW OPUS" if res["mode"] == "raw" else "OGG OPUS"
        print(f"\n{mode_name}:")
        print(f"  encoder_out_bytes: {res['encoder_out_bytes']}")
        print(f"  engine_sent_frames: {res['engine_sent_frames']}")
        print(f"  engine_recv_audio_frames: {res['engine_recv_audio_frames']}")
        print(f"  engine_recv_text_frames: {res['engine_recv_text_frames']}")
        print(f"  engine_close_code: {res['engine_close_code']}")
        print(f"  engine_close_reason: {res['engine_close_reason']}")
        if res["error"]:
            print(f"  ERROR: {res['error']}")
        if res["ffmpeg_stderr"]:
            print(f"  FFmpeg stderr (first 200 chars): {res['ffmpeg_stderr'][:200]}")
    
    # Decision logic
    print("\n" + "=" * 60)
    print("VERDICT")
    print("=" * 60)
    
    raw_works = (results_raw["encoder_out_bytes"] > 0 and 
                 results_raw["engine_recv_audio_frames"] > 10)
    ogg_works = (results_ogg["encoder_out_bytes"] > 0 and 
                 results_ogg["engine_recv_audio_frames"] > 10)
    
    if raw_works:
        print("✓ RAW OPUS works!")
        print("  → Bridge should switch to raw opus format")
    elif ogg_works:
        print("✓ OGG OPUS works!")
        print("  → Bridge needs buffering/read strategy fix only")
    else:
        print("✗ Neither mode produced >10 audio frames")
        print("  → Engine framing mismatch or encoder issue")
        if results_raw["encoder_out_bytes"] == 0 and results_ogg["encoder_out_bytes"] == 0:
            print("  → Encoder produced no output (buffering issue)")
        elif results_raw["encoder_out_bytes"] > 0 or results_ogg["encoder_out_bytes"] > 0:
            print("  → Encoder produced output but engine didn't respond")
    
    # Save results
    results_path = artifacts_dir / "test_results.txt"
    with open(results_path, "w") as f:
        f.write("Test Results\n")
        f.write("=" * 60 + "\n\n")
        for res in all_results:
            mode_name = "RAW OPUS" if res["mode"] == "raw" else "OGG OPUS"
            f.write(f"{mode_name}:\n")
            for key, value in res.items():
                f.write(f"  {key}: {value}\n")
            f.write("\n")
    print(f"\n✓ Saved results to: {results_path}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nFATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
