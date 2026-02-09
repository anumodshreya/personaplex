import asyncio
import websockets
import json
import base64
import numpy as np
import time
import sys
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("DiagnosticTest")

# Configuration
WS_URL = "ws://localhost:5050/media-stream"
SAMPLE_RATE = 8000
FRAME_DURATION = 0.02  # 20ms
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION)  # 160 samples
SPEECH_DURATION = 1.0  # 1 second speech
SILENCE_DURATION = 10.0 # 10 seconds of silence to observe engine response

def generate_tone(duration, freq):
    t = np.linspace(0, duration, int(SAMPLE_RATE * duration), endpoint=False)
    audio = 0.5 * np.sin(2 * np.pi * freq * t)
    return (audio * 32767).astype(np.int16).tobytes()

def generate_silence(duration):
    return np.zeros(int(SAMPLE_RATE * duration), dtype=np.int16).tobytes()

async def send_audio_stream(ws):
    # 1. Send Speech
    speech_data = generate_tone(SPEECH_DURATION, 440)
    total_speech_frames = len(speech_data) // (FRAME_SIZE * 2)
    
    logger.info(f"Sending {SPEECH_DURATION}s of speech...")
    start_time = time.time()
    
    for i in range(total_speech_frames):
        chunk = speech_data[i * FRAME_SIZE * 2 : (i + 1) * FRAME_SIZE * 2]
        payload = base64.b64encode(chunk).decode('utf-8')
        msg = {
            "event": "media",
            "media": {
                "payload": payload,
                "timestamp": str(int(time.time() * 1000))
            }
        }
        await ws.send(json.dumps(msg))
        await asyncio.sleep(FRAME_DURATION)
        
    # 2. Send Silence
    silence_data = generate_silence(SILENCE_DURATION)
    total_silence_frames = len(silence_data) // (FRAME_SIZE * 2)
    
    logger.info(f"Sending {SILENCE_DURATION}s of silence to keep connection open...")
    
    for i in range(total_silence_frames):
        chunk = silence_data[i * FRAME_SIZE * 2 : (i + 1) * FRAME_SIZE * 2]
        payload = base64.b64encode(chunk).decode('utf-8')
        msg = {
            "event": "media",
            "media": {
                "payload": payload,
                "timestamp": str(int(time.time() * 1000))
            }
        }
        try:
            await ws.send(json.dumps(msg))
            await asyncio.sleep(FRAME_DURATION)
        except websockets.exceptions.ConnectionClosed:
            logger.warning("Connection closed while sending silence")
            return

    logger.info("Finished sending silence.")

async def run_diagnostic():
    try:
        async with websockets.connect(WS_URL) as ws:
            logger.info("Connected to Bridge.")
            
            # Start sender
            sender_task = asyncio.create_task(send_audio_stream(ws))
            
            # Listen for responses
            first_response_ts = None
            last_response_ts = None
            response_count = 0
            
            start_listening = time.time()
            
            try:
                while True:
                    # Wait up to 15 seconds for messages (covering speech + silence)
                    msg_raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
                    now = time.time()
                    
                    if not first_response_ts:
                        first_response_ts = now
                        latency = (first_response_ts - start_listening) * 1000
                        logger.info(f"FIRST_RESPONSE_RECEIVED: Latency approx {latency:.2f}ms (from connect/listen start)")
                    
                    try:
                        data = json.loads(msg_raw)
                        if data.get('event') == 'media':
                            response_count += 1
                            if last_response_ts:
                                gap = (now - last_response_ts) * 1000
                                if gap > 1000:
                                    logger.warning(f"GAP_DETECTED: {gap:.2f}ms between media frames")
                            last_response_ts = now
                        elif data.get('event') == 'stop':
                             logger.info("Received STOP event from bridge")
                        elif data.get('event') == 'mark':
                             logger.info(f"Received MARK: {data}")
                    except json.JSONDecodeError:
                         # Binary
                         pass
                         
                    if sender_task.done() and (time.time() - start_listening > 12):
                        # Stop after sufficient time
                        break
                        
            except asyncio.TimeoutError:
                logger.info("Timeout waiting for response")
            except websockets.exceptions.ConnectionClosed as e:
                logger.info(f"Connection closed: {e.code} {e.reason}")
            
            if not sender_task.done():
                sender_task.cancel()
                
            logger.info(f"Diagnostic Complete. Total Media Responses: {response_count}")

    except Exception as e:
        logger.error(f"Test failed: {e}")

if __name__ == "__main__":
    asyncio.run(run_diagnostic())
