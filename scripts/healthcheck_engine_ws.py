#!/usr/bin/env python3
"""Health check script for PersonaPlex engine WebSocket endpoint."""
import asyncio
import ssl
import sys

try:
    import websockets
except ImportError:
    print("ERROR: websockets not installed. Run: pip install websockets")
    sys.exit(1)


async def test_engine_ws():
    """Test WebSocket connection to PersonaPlex engine."""
    url = "wss://localhost:8998/api/chat?voice_prompt=NATF0.pt&text_prompt=hi"
    
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    
    try:
        print(f"Connecting to: {url}")
        async with websockets.connect(url, ssl=ssl_ctx, open_timeout=10) as ws:
            print("✓ CONNECTED to PersonaPlex engine")
            
            # Wait for handshake (0x00 byte)
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=15.0)
                if isinstance(msg, bytes):
                    print(f"✓ Received handshake: {msg.hex()} (type: {msg[0] if len(msg) > 0 else 'empty'})")
                else:
                    print(f"✓ Received: {msg}")
                print("✓ Engine WebSocket health check PASSED")
                return 0
            except asyncio.TimeoutError:
                print("✗ Timeout waiting for handshake")
                return 1
    except Exception as e:
        print(f"✗ Connection failed: {e}")
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(test_engine_ws())
    sys.exit(exit_code)
