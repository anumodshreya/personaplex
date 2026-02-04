import asyncio, json, base64, os
import websockets

PORT = int(os.getenv("BRIDGE_PORT", "5050"))

# 100ms of silence at 8kHz PCM16 mono = 800 samples * 2 bytes = 1600 bytes
SILENCE_100MS = b"\x00" * 1600

async def handler(ws):
    print("[bridge] connected")
    try:
        async for msg in ws:
            # Exotel sends JSON strings
            try:
                data = json.loads(msg)
            except Exception:
                print("[bridge] non-json", type(msg))
                continue

            event = data.get("event") or data.get("type")
            if event:
                print("[bridge] event:", event)

            # For now: if we receive media, we respond with silence media
            if event == "media":
                media = data.get("media") or {}
                payload = media.get("payload")
                if payload:
                    raw = base64.b64decode(payload)
                    print("[bridge] media bytes:", len(raw))

                out = {"event": "media", "media": {"payload": base64.b64encode(SILENCE_100MS).decode("ascii")}}
                await ws.send(json.dumps(out))

            if event == "stop":
                print("[bridge] stop")
                break

    except websockets.ConnectionClosed as e:
        print("[bridge] closed", e.code, e.reason)
    finally:
        print("[bridge] disconnected")

async def main():
    print(f"[bridge] listening on 0.0.0.0:{PORT}")
    async with websockets.serve(handler, "0.0.0.0", PORT, max_size=10_000_000):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
