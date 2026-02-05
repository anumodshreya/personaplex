#!/usr/bin/env python3
"""
Engine WebSocket golden path test - uses known-good approach from test_engine_dummy.py.
Tests engine independently of bridge to rule out engine bugs.
"""
import asyncio
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import quote

try:
    import websockets
    import numpy as np
except ImportError:
    print("ERROR: Missing dependencies. Install: pip install websockets numpy")
    sys.exit(1)


async def test_engine_ws_golden():
    """Test engine WebSocket using known-good sphn.OpusStreamWriter approach."""
    text_prompt = "You enjoy having a good conversation."
    url = f"wss://127.0.0.1:8998/api/chat?voice_prompt=NATF0.pt&text_prompt={quote(text_prompt)}"
    
    print("=" * 60)
    print("Engine WebSocket Golden Path Test")
    print("=" * 60)
    print(f"Connecting to: {url}")
    
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    
    audio_frames = 0
    text_frames = 0
    total_bytes = 0
    audio_payloads = []
    
    artifacts_dir = Path("/workspace/personaplex/artifacts")
    artifacts_dir.mkdir(exist_ok=True)
    
    try:
        async with websockets.connect(url, ssl=ssl_ctx, open_timeout=10) as ws:
            print("✓ CONNECTED")
            
            # Wait for handshake
            handshake_received = False
            for _ in range(50):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.3)
                    if isinstance(msg, bytes) and len(msg) > 0 and msg[0] == 0x00:
                        print("✓ Received handshake (0x00)")
                        handshake_received = True
                        break
                except asyncio.TimeoutError:
                    continue
            
            if not handshake_received:
                print("✗ Did not receive handshake")
                return False
            
            # Use FFmpeg to encode (same as bridge, known to work)
            import subprocess
            ffmpeg_cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0",
                "-c:a", "libopus", "-application", "voip", "-frame_duration", "20",
                "-vbr", "off", "-b:a", "24k", "-f", "ogg", "pipe:1"
            ]
            proc = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )
            
            # Send 1 second of silence (PCM16LE)
            print("\nSending 1 second of silence...")
            chunk_samples = 480  # 20ms @ 24kHz
            chunk_bytes = chunk_samples * 2  # 16-bit = 2 bytes per sample
            num_chunks = int(24000 * 1.0 / chunk_samples)  # 50 chunks for 1 second
            
            silence_pcm = np.zeros(chunk_samples, dtype=np.int16).tobytes()
            for i in range(num_chunks):
                proc.stdin.write(silence_pcm)
                proc.stdin.flush()
                # Read encoded output
                opus_bytes = proc.stdout.read(4096)
                if opus_bytes:
                    await ws.send(b"\x01" + opus_bytes)
                await asyncio.sleep(0.02)
            
            # Send silence tail (500ms) to trigger response
            print("Sending 500ms silence tail...")
            tail_chunks = int(24000 * 0.5 / chunk_samples)  # 25 chunks for 500ms
            for i in range(tail_chunks):
                proc.stdin.write(silence_pcm)
                proc.stdin.flush()
                opus_bytes = proc.stdout.read(4096)
                if opus_bytes:
                    await ws.send(b"\x01" + opus_bytes)
                await asyncio.sleep(0.02)
            
            proc.stdin.close()
            # Drain remaining
            while True:
                opus_bytes = proc.stdout.read(4096)
                if not opus_bytes:
                    break
                await ws.send(b"\x01" + opus_bytes)
            proc.wait()
            
            print("\nListening for responses (10 seconds)...")
            start_time = time.time()
            timeout = 10.0
            
            while time.time() - start_time < timeout:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    if not isinstance(msg, bytes) or len(msg) == 0:
                        continue
                    
                    frame_type = msg[0]
                    payload = msg[1:] if len(msg) > 1 else b""
                    
                    if frame_type == 0x01:
                        audio_frames += 1
                        total_bytes += len(payload)
                        audio_payloads.append(payload)
                        if audio_frames <= 5:
                            print(f"  Audio frame {audio_frames}: {len(payload)} bytes")
                    elif frame_type == 0x02:
                        text_frames += 1
                        try:
                            text = payload.decode("utf-8", errors="ignore")
                            if text_frames <= 3:
                                print(f"  Text frame {text_frames}: '{text[:50]}'")
                        except Exception:
                            pass
                    elif frame_type == 0x00:
                        # Keepalive, ignore
                        pass
                    
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    print(f"  Error receiving: {e}")
                    break
            
            # Save concatenated audio payloads
            if audio_payloads:
                concat_audio = b"".join(audio_payloads)
                output_path = artifacts_dir / "ws_engine_concat.bin"
                with open(output_path, "wb") as f:
                    f.write(concat_audio)
                print(f"\n✓ Saved {len(concat_audio)} bytes to {output_path}")
            
            print("\n" + "=" * 60)
            print("Results:")
            print("=" * 60)
            print(f"audio_frames: {audio_frames}")
            print(f"text_frames: {text_frames}")
            print(f"total_bytes: {total_bytes}")
            
            success = (audio_frames >= 10) or (text_frames > 0)
            if success:
                print("\n✓ ENGINE RESPONDED (PASS)")
            else:
                print("\n✗ ENGINE DID NOT RESPOND ADEQUATELY (FAIL)")
            
            return success
    
    except Exception as e:
        print(f"\n✗ Connection failed: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = asyncio.run(test_engine_ws_golden())
    sys.exit(0 if success else 1)
