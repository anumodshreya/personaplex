#!/usr/bin/env python3
"""
Test PersonaPlex engine directly (no bridge).

Connects to engine WebSocket and sends dummy audio to verify it responds.
"""
import asyncio
import ssl
import sys
import time

try:
    import websockets
    import numpy as np
    import sphn
except ImportError:
    print("ERROR: Missing dependencies. Install: pip install websockets numpy sphn")
    sys.exit(1)


async def test_engine_direct():
    """Test engine directly via WebSocket."""
    from urllib.parse import quote
    text_prompt = "You enjoy having a good conversation. Say the word 'HELLO' clearly."
    url = f"wss://localhost:8998/api/chat?voice_prompt=NATF0.pt&text_prompt={quote(text_prompt)}"
    
    print("=" * 60)
    print("PHASE 1: Direct Engine Test")
    print("=" * 60)
    print(f"Connecting to: {url}")
    
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    
    audio_frames = 0
    text_frames = 0
    first_text_payload = None
    
    try:
        async with websockets.connect(url, ssl=ssl_ctx, open_timeout=10) as ws:
            print("CONNECTED ✓")
            
            # Wait for handshake
            handshake_received = False
            for _ in range(50):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=0.3)
                    if isinstance(msg, bytes) and len(msg) > 0 and msg[0] == 0x00:
                        print("Received handshake (0x00)")
                        handshake_received = True
                        break
                except asyncio.TimeoutError:
                    continue
            
            if not handshake_received:
                print("✗ Did not receive handshake")
                return False
            
            # Initialize Opus encoder
            opus_writer = sphn.OpusStreamWriter(24000)
            
            # Send 1 second of silence (to trigger response)
            # Opus requires frame sizes: [120, 240, 480, 960, 1920, 2880] samples
            # 20ms @ 24kHz = 480 samples (valid)
            print("\nSending 1 second of silence...")
            chunk_samples = 480  # 20ms @ 24kHz (valid Opus frame size)
            num_chunks = int(24000 * 1.0 / chunk_samples)  # 50 chunks for 1 second
            
            for i in range(num_chunks):
                chunk = np.zeros(chunk_samples, dtype=np.float32)
                opus_writer.append_pcm(chunk)
                opus_bytes = opus_writer.read_bytes()
                if opus_bytes:
                    await ws.send(b"\x01" + opus_bytes)
                await asyncio.sleep(0.02)
            
            # Send silence tail (500ms) to trigger response
            print("Sending 500ms silence tail...")
            tail_chunks = int(24000 * 0.5 / chunk_samples)  # 25 chunks for 500ms
            for i in range(tail_chunks):
                chunk = np.zeros(chunk_samples, dtype=np.float32)
                opus_writer.append_pcm(chunk)
                opus_bytes = opus_writer.read_bytes()
                if opus_bytes:
                    await ws.send(b"\x01" + opus_bytes)
                await asyncio.sleep(0.02)
            
            print("\nListening for responses (15 seconds)...")
            start_time = time.time()
            timeout = 15.0
            
            while time.time() - start_time < timeout:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    if not isinstance(msg, bytes) or len(msg) == 0:
                        continue
                    
                    frame_type = msg[0]
                    payload = msg[1:] if len(msg) > 1 else b""
                    
                    if frame_type == 0x01:
                        audio_frames += 1
                        if audio_frames <= 3:
                            print(f"  Received audio frame {audio_frames}: {len(payload)} bytes")
                    elif frame_type == 0x02:
                        text_frames += 1
                        try:
                            text = payload.decode("utf-8", errors="ignore")
                            if first_text_payload is None:
                                first_text_payload = text
                            if text_frames <= 3:
                                print(f"  Received text frame {text_frames}: '{text}'")
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
            
            print("\n" + "=" * 60)
            print("Results:")
            print("=" * 60)
            print(f"audio_frames={audio_frames}")
            print(f"text_frames={text_frames}")
            if first_text_payload:
                print(f"first_text_payload='{first_text_payload[:100]}'")
            
            success = (audio_frames > 0) or (text_frames > 0)
            if success:
                print("\n✓ ENGINE RESPONDED")
            else:
                print("\n✗ ENGINE DID NOT RESPOND — STOP HERE")
            
            return success
    
    except Exception as e:
        print(f"\n✗ Connection failed: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = asyncio.run(test_engine_direct())
    sys.exit(0 if success else 1)
