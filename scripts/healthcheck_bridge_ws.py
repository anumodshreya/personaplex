#!/usr/bin/env python3
"""Health check script for Exotel bridge WebSocket endpoint."""
import asyncio
import base64
import json
import sys

try:
    import websockets
except ImportError:
    print("ERROR: websockets not installed. Run: pip install websockets")
    sys.exit(1)


async def test_bridge_ws():
    """Test WebSocket connection to Exotel bridge."""
    url = "ws://localhost:5050"
    
    try:
        print(f"Connecting to: {url}")
        async with websockets.connect(url, open_timeout=10) as ws:
            print("✓ CONNECTED to Exotel bridge")
            
            # Send start event
            start_event = {"event": "start"}
            await ws.send(json.dumps(start_event))
            print(f"✓ Sent start event: {start_event}")
            
            # Send media event with 100ms silence (8kHz PCM16 mono = 1600 bytes)
            silence = b"\x00" * 1600
            media_event = {
                "event": "media",
                "media": {"payload": base64.b64encode(silence).decode("ascii")}
            }
            await ws.send(json.dumps(media_event))
            print(f"✓ Sent media event (1600 bytes silence)")
            
            # Listen for responses (with timeout)
            responses = []
            try:
                for _ in range(5):  # Check up to 5 responses
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        responses.append(msg)
                        print(f"✓ Received response: {msg[:100]}..." if len(str(msg)) > 100 else f"✓ Received response: {msg}")
                    except asyncio.TimeoutError:
                        break
            except Exception as e:
                print(f"Note: Error receiving responses: {e}")
            
            if responses:
                print(f"✓ Bridge responded with {len(responses)} message(s)")
            else:
                print("⚠ No responses received (bridge may be processing or waiting for engine)")
            
            print("✓ Bridge WebSocket health check PASSED")
            return 0
    except Exception as e:
        print(f"✗ Connection failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(test_bridge_ws())
    sys.exit(exit_code)
