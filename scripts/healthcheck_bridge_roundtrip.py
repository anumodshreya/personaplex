#!/usr/bin/env python3
"""
Roundtrip health check for Exotel bridge.

Tests bidirectional audio flow:
1. Connect to bridge
2. Send start event
3. Send test audio frames (8kHz PCM tone + silence tail)
4. Receive audio frames from engine (via bridge)
5. Verify roundtrip works
"""
import asyncio
import base64
import json
import math
import struct
import sys

try:
    import websockets
except ImportError:
    print("ERROR: websockets not installed. Run: pip install websockets")
    sys.exit(1)


def generate_tone_pcm(frequency: int, duration_ms: int, sample_rate: int = 8000) -> bytes:
    """Generate a sine wave tone as PCM16LE bytes."""
    num_samples = int(sample_rate * duration_ms / 1000)
    samples = []
    for i in range(num_samples):
        t = i / sample_rate
        value = int(32767 * 0.3 * math.sin(2 * math.pi * frequency * t))
        samples.append(struct.pack('<h', value))
    return b''.join(samples)


async def test_roundtrip():
    """Test bidirectional audio flow through bridge."""
    url = "ws://localhost:5050"
    
    print("=" * 60)
    print("Bridge Roundtrip Health Check")
    print("=" * 60)
    
    try:
        print(f"Connecting to: {url}")
        async with websockets.connect(url, open_timeout=10) as ws:
            print("✓ CONNECTED to Exotel bridge")
            
            # Send start event
            start_event = {"event": "start"}
            await ws.send(json.dumps(start_event))
            print("✓ Sent start event")
            
            # Wait a bit for bridge to connect to engine
            await asyncio.sleep(2)
            
            # Send test audio frames
            frames_sent = 0
            total_bytes_sent = 0
            
            print("\nSending test audio frames...")
            
            # Send 1 second of clear tone (440Hz)
            print("  - Sending 1s tone burst (440Hz)...")
            tone_100ms = generate_tone_pcm(440, 100, 8000)
            for i in range(10):
                media_frame = {
                    "event": "media",
                    "media": {"payload": base64.b64encode(tone_100ms).decode("ascii")}
                }
                await ws.send(json.dumps(media_frame))
                frames_sent += 1
                total_bytes_sent += len(tone_100ms)
                await asyncio.sleep(0.1)  # Real-time pacing
            
            # Send 1 second of silence tail (to trigger engine response)
            print("  - Sending 1s silence tail (to trigger engine response)...")
            silence_100ms = b"\x00" * 1600  # 100ms @ 8kHz = 1600 bytes
            for i in range(10):
                media_frame = {
                    "event": "media",
                    "media": {"payload": base64.b64encode(silence_100ms).decode("ascii")}
                }
                await ws.send(json.dumps(media_frame))
                frames_sent += 1
                total_bytes_sent += len(silence_100ms)
                await asyncio.sleep(0.1)  # Real-time pacing
            
            print(f"✓ Sent {frames_sent} frames ({total_bytes_sent} bytes)")
            
            # Listen for responses (up to 15 seconds)
            print("\nListening for responses from engine (via bridge)...")
            print("  (Listening for up to 15 seconds)")
            frames_received = 0
            total_bytes_received = 0
            payload_lengths = []
            timeout_seconds = 15
            start_time = asyncio.get_event_loop().time()
            
            try:
                while True:
                    elapsed = asyncio.get_event_loop().time() - start_time
                    if elapsed > timeout_seconds:
                        break
                    
                    try:
                        timeout = max(0.1, timeout_seconds - elapsed)
                        msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
                        
                        try:
                            data = json.loads(msg)
                            if data.get("event") == "media":
                                payload = data.get("media", {}).get("payload")
                                if payload:
                                    decoded = base64.b64decode(payload)
                                    frames_received += 1
                                    total_bytes_received += len(decoded)
                                    payload_lengths.append(len(decoded))
                                    if frames_received <= 3:
                                        print(f"  ✓ Received frame {frames_received}: {len(decoded)} bytes")
                        except json.JSONDecodeError:
                            pass
                    except asyncio.TimeoutError:
                        break
            except Exception as e:
                print(f"Note: Error receiving: {e}")
            
            # Results
            print("\n" + "=" * 60)
            print("Roundtrip Test Results")
            print("=" * 60)
            print(f"Frames sent:     {frames_sent}")
            print(f"Bytes sent:       {total_bytes_sent}")
            print(f"Frames received:  {frames_received}")
            print(f"Bytes received:   {total_bytes_received}")
            
            if payload_lengths:
                print(f"\nFirst 3 payload sizes: {payload_lengths[:3]}")
            
            if frames_received > 0:
                print("\n✓ ROUNDTRIP TEST PASSED")
                print("  Engine is responding with audio!")
                return 0
            else:
                print("\n✗ ROUNDTRIP TEST FAILED")
                print("  No audio frames received from engine")
                print("\nDiagnostic suggestions:")
                print("  1. Check engine is running: ss -lntp | grep 8998")
                print("  2. Check bridge logs: tail -f logs/bridge.log")
                print("  3. Verify ffmpeg is installed: ffmpeg -version")
                print("  4. Check engine connection in bridge logs")
                print("  5. Verify sample rates match (MODEL_SR=24000)")
                return 1
    
    except Exception as e:
        print(f"\n✗ Connection failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(test_roundtrip())
    sys.exit(exit_code)
