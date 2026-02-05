#!/usr/bin/env python3
"""
Phase 2: Capture Engine WebSocket Audio Payloads
Captures exact 0x01 payload bytes from engine WS and attempts offline decode.
"""
import asyncio
import ssl
import sys
import time
from urllib.parse import quote_plus

try:
    import websockets
except ImportError:
    print("ERROR: websockets not installed")
    sys.exit(1)


async def capture_engine_ws():
    """Capture engine WS audio frames."""
    text_prompt = "You enjoy having a good conversation. Say HELLO."
    url = f"wss://localhost:8998/api/chat?voice_prompt=NATF0.pt&text_prompt={quote_plus(text_prompt)}"
    
    print("=== PHASE 2: Direct Engine WS Capture ===")
    print(f"Connecting to: {url}")
    
    # SSL context
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    
    audio_frames = 0
    audio_bytes = 0
    text_frames = 0
    
    try:
        async with websockets.connect(url, ssl=ssl_ctx, open_timeout=15) as ws:
            print("CONNECTED ✓")
            
            # Wait for handshake first
            handshake_received = False
            for _ in range(10):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    if isinstance(msg, bytes) and len(msg) > 0 and msg[0] == 0x00:
                        print("Received handshake (0x00)")
                        handshake_received = True
                        break
                except asyncio.TimeoutError:
                    continue
            
            if not handshake_received:
                print("✗ Did not receive handshake")
                return False
            
            # Send 1 second of silence to trigger response
            import numpy as np
            import sphn
            
            opus_writer = sphn.OpusStreamWriter(24000)
            # Opus frame size: 480 samples = 20ms @ 24kHz (valid frame size)
            chunk_samples = 480
            num_chunks = int(24000 * 1.0 / chunk_samples)  # 50 chunks for 1 second
            
            print("Sending 1 second of silence...")
            for i in range(num_chunks):
                chunk = np.zeros(chunk_samples, dtype=np.float32)
                opus_writer.append_pcm(chunk)
                opus_bytes = opus_writer.read_bytes()
                if opus_bytes:
                    await ws.send(b"\x01" + opus_bytes)
                await asyncio.sleep(0.02)
            
            # Send silence tail (500ms)
            print("Sending 500ms silence tail...")
            tail_chunks = int(24000 * 0.5 / chunk_samples)  # 25 chunks for 500ms
            for i in range(tail_chunks):
                chunk = np.zeros(chunk_samples, dtype=np.float32)
                opus_writer.append_pcm(chunk)
                opus_bytes = opus_writer.read_bytes()
                if opus_bytes:
                    await ws.send(b"\x01" + opus_bytes)
                await asyncio.sleep(0.02)
            
            print("Listening for 20 seconds...")
            
            # Open output file
            with open("logs/engine_ws_audio_payload.bin", "wb") as audio_file, \
                 open("logs/engine_ws_text.txt", "w") as text_file:
                
                start_time = time.time()
                timeout = 20.0
                
                while time.time() - start_time < timeout:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        print(f"Error receiving: {e}")
                        break
                    
                    if not isinstance(msg, (bytes, bytearray)) or len(msg) == 0:
                        continue
                    
                    frame_type = msg[0]
                    payload = msg[1:] if len(msg) > 1 else b""
                    
                    if frame_type == 0x01:
                        audio_file.write(payload)
                        audio_frames += 1
                        audio_bytes += len(payload)
                        if audio_frames <= 5:
                            print(f"  Audio frame {audio_frames}: {len(payload)} bytes")
                    elif frame_type == 0x02:
                        try:
                            text = payload.decode("utf-8", errors="ignore")
                            text_file.write(text + "\n")
                            text_frames += 1
                            if text_frames <= 3:
                                print(f"  Text frame {text_frames}: {text[:50]}")
                        except Exception:
                            pass
    
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    print("\nResults:")
    print(f"audio_frames={audio_frames}")
    print(f"audio_bytes={audio_bytes}")
    print(f"text_frames={text_frames}")
    
    import os
    if os.path.exists("logs/engine_ws_audio_payload.bin"):
        size = os.path.getsize("logs/engine_ws_audio_payload.bin")
        print(f"File size: {size} bytes")
    
    return audio_frames > 0


if __name__ == "__main__":
    success = asyncio.run(capture_engine_ws())
    sys.exit(0 if success else 1)
