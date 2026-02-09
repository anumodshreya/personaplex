import asyncio
import websockets
import json
import sys

async def test_connection(url):
    print(f"Testing connection to: {url}")
    try:
        async with websockets.connect(url) as ws:
            print("Successfully connected and performed handshake!")
            # Try to send a stop event or just wait
            print("Sending test message...")
            await ws.send(json.dumps({"event": "start", "start": {"streamSid": "test_sid"}}))
            print("Message sent. Waiting for response (if any)...")
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=2)
                print(f"Received: {msg}")
            except asyncio.TimeoutError:
                print("No response received (which is normal if no audio yet).")
            return True
    except Exception as e:
        print(f"Failed to connect: {e}")
        return False

if __name__ == "__main__":
    target_url = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:5050"
    asyncio.run(test_connection(target_url))
