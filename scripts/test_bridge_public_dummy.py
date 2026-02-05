#!/usr/bin/env python3
"""
Test Exotel bridge via public Cloudflare tunnel.

Same as local test but connects through public WSS URL.
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


async def test_bridge_public():
    """Test bridge via public tunnel."""
    url = "wss://eleven-aggregate-task-scheme.trycloudflare.com"
    
    print("=" * 60)
    print("PHASE 3: Public Tunnel Test")
    print("=" * 60)
    print(f"Connecting to: {url}")
    
    sent_frames = 0
    received_frames = 0
    first_payload_size = None
    time_to_first_frame = None
    
    try:
        connect_start = time.time()
        async with websockets.connect(url, open_timeout=15) as ws:
            connect_time = time.time() - connect_start
            print(f"CONNECTED ✓ (took {connect_time:.2f}s)")
            
            # Send start event
            start_event = {"event": "start"}
            await ws.send(json.dumps(start_event))
            print("Sent start event")
            
            # Wait for bridge to connect to engine
            await asyncio.sleep(2)
            
            # Send 1 second of PCM8k silence
            print("\nSending 1 second of PCM8k silence...")
            silence_100ms = b"\x00" * 1600  # 100ms @ 8kHz = 1600 bytes
            for i in range(10):
                media_frame = {
                    "event": "media",
                    "media": {"payload": base64.b64encode(silence_100ms).decode("ascii")}
                }
                await ws.send(json.dumps(media_frame))
                sent_frames += 1
                await asyncio.sleep(0.1)
            
            # Send 1 second of silence tail
            print("Sending 1 second silence tail...")
            for i in range(10):
                media_frame = {
                    "event": "media",
                    "media": {"payload": base64.b64encode(silence_100ms).decode("ascii")}
                }
                await ws.send(json.dumps(media_frame))
                sent_frames += 1
                await asyncio.sleep(0.1)
            
            print(f"\nSent {sent_frames} frames")
            print("Listening for responses (20 seconds)...")
            
            listen_start = time.time()
            timeout = 20.0
            
            while time.time() - listen_start < timeout:
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
                                    time_to_first_frame = time.time() - listen_start
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
            if time_to_first_frame:
                print(f"time_to_first_frame={time_to_first_frame:.2f}s")
            
            success = received_frames > 0
            if success:
                print("\n✓ PUBLIC TUNNEL RETURNED FRAMES")
            else:
                print("\n✗ PUBLIC TUNNEL RETURNED ZERO FRAMES")
            
            return success
    
    except Exception as e:
        print(f"\n✗ Connection failed: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = asyncio.run(test_bridge_public())
    sys.exit(0 if success else 1)
