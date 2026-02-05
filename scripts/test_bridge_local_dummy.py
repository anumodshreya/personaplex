#!/usr/bin/env python3
"""
Test Exotel bridge locally with dummy prompts.

Connects to local bridge and sends dummy audio to verify bridge → engine → bridge → client flow.
"""
import asyncio
import base64
import json
import sys
import time

try:
    import websockets
except ImportError:
    print("ERROR: websockets not installed. Run: pip install websockets")
    sys.exit(1)


async def test_bridge_local():
    """Test bridge locally."""
    url = "ws://localhost:5050"
    
    print("=" * 60)
    print("PHASE 2: Local Bridge Test")
    print("=" * 60)
    print(f"Connecting to: {url}")
    
    sent_frames = 0
    received_frames = 0
    first_payload_size = None
    
    try:
        async with websockets.connect(url, open_timeout=10) as ws:
            print("CONNECTED ✓")
            
            # Send start event
            start_event = {"event": "start"}
            await ws.send(json.dumps(start_event))
            print("Sent start event")
            
            # Wait for bridge to connect to engine
            await asyncio.sleep(2)
            
            # Generate 440Hz tone at 8kHz PCM16LE (20ms frames = 320 bytes)
            import numpy as np
            sample_rate = 8000
            tone_freq = 440
            frame_duration = 0.02  # 20ms
            samples_per_frame = int(sample_rate * frame_duration)  # 160 samples
            
            def generate_tone_frame():
                t = np.linspace(0, frame_duration, samples_per_frame, False)
                tone = np.sin(2 * np.pi * tone_freq * t)
                pcm16 = (tone * 32767).astype(np.int16)
                return pcm16.tobytes()
            
            # Send 2 seconds of 440Hz tone (100 frames @ 20ms each)
            print("\nSending 2 seconds of 440Hz tone (100 frames @ 20ms)...")
            for i in range(100):
                tone_frame = generate_tone_frame()
                media_frame = {
                    "event": "media",
                    "media": {"payload": base64.b64encode(tone_frame).decode("ascii")}
                }
                await ws.send(json.dumps(media_frame))
                sent_frames += 1
                await asyncio.sleep(0.02)  # 20ms between frames
            
            # Send 500ms silence tail (25 frames)
            print("Sending 500ms silence tail...")
            silence_frame = b"\x00" * 320  # 20ms @ 8kHz = 320 bytes
            for i in range(25):
                media_frame = {
                    "event": "media",
                    "media": {"payload": base64.b64encode(silence_frame).decode("ascii")}
                }
                await ws.send(json.dumps(media_frame))
                sent_frames += 1
                await asyncio.sleep(0.02)
            
            print(f"\nSent {sent_frames} frames")
            print("Listening for responses (15 seconds)...")
            
            start_time = time.time()
            timeout = 15.0
            
            while time.time() - start_time < timeout:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    try:
                        data = json.loads(msg)
                        if data.get("event") == "media":
                            payload = data.get("media", {}).get("payload")
                            if payload:
                                decoded = base64.b64decode(payload)
                                received_frames += 1
                                if first_payload_size is None:
                                    first_payload_size = len(decoded)
                                if received_frames <= 3:
                                    print(f"  Received frame {received_frames}: {len(decoded)} bytes")
                    except json.JSONDecodeError:
                        pass
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    print(f"  Error receiving: {e}")
                    break
            
            print("\n" + "=" * 60)
            print("Results:")
            print("=" * 60)
            print(f"sent_frames={sent_frames}")
            print(f"received_frames={received_frames}")
            if first_payload_size:
                print(f"first_payload_size={first_payload_size}")
            
            success = received_frames > 0
            if success:
                print("\n✓ PASS: Bridge returned frames")
            else:
                print("\n✗ FAIL: Bridge returned zero frames")
                # Dump last 200 lines of logs
                log_path = "/workspace/personaplex/logs/bridge.log"
                try:
                    with open(log_path, 'r') as f:
                        lines = f.readlines()
                        last_lines = lines[-200:] if len(lines) > 200 else lines
                        print(f"\nLast 200 lines of {log_path}:")
                        print("=" * 60)
                        print("".join(last_lines))
                        print("=" * 60)
                except Exception as e:
                    print(f"Could not read log file: {e}")
            
            return success
    
    except Exception as e:
        print(f"\n✗ Connection failed: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = asyncio.run(test_bridge_local())
    sys.exit(0 if success else 1)
