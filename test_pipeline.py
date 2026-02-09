import asyncio
import websockets
import json
import base64
import numpy as np
import scipy.io.wavfile as wav
import time
import sys
import logging

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("PipelineTest")

# Configuration
WS_URL = "ws://localhost:5050/media-stream" 
SAMPLE_RATE = 8000
CHANNELS = 1
DURATION = 2  # seconds
FREQUENCY = 440  # Hz
FRAME_DURATION = 0.02  # 20ms
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION)  # 160 samples

def generate_dummy_audio():
    """Generates a 2-second mono 8kHz PCM16LE sine wave."""
    t = np.linspace(0, DURATION, int(SAMPLE_RATE * DURATION), endpoint=False)
    audio = 0.5 * np.sin(2 * np.pi * FREQUENCY * t)
    
    # Append 1 second of silence
    silence = np.zeros(int(SAMPLE_RATE * 1.0), dtype=np.float64)
    audio_combined = np.concatenate((audio, silence))
    
    audio_int16 = (audio_combined * 32767).astype(np.int16)
    return audio_int16.tobytes()

async def send_audio(ws, audio_data):
    """Streams audio data in chunks."""
    total_frames = len(audio_data) // (FRAME_SIZE * 2) # 2 bytes per sample
    logger.info(f"Starting stream. Total frames to send: {total_frames}")
    
    start_time = time.time()
    
    for i in range(total_frames):
        chunk = audio_data[i * FRAME_SIZE * 2 : (i + 1) * FRAME_SIZE * 2]
        
        payload = base64.b64encode(chunk).decode('utf-8')
        message = {
            "event": "media",
            "media": {
                "payload": payload,
                "timestamp": str(int(time.time() * 1000))
            }
        }
        
        try:
            await ws.send(json.dumps(message))
            await asyncio.sleep(FRAME_DURATION)
        except Exception as e:
            logger.error(f"Error sending frame {i}: {e}")
            break
            
    logger.info(f"Finished streaming in {time.time() - start_time:.2f}s")

async def receive_messages(ws):
    """Listens for responses from the server."""
    logger.info("Listening for responses...")
    first_response_time = None
    
    try:
        while True:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=5.0)
                if not first_response_time:
                    first_response_time = time.time()
                    logger.info("First response received!")

                # Try to decode if it's JSON, otherwise treat as binary
                try:
                    data = json.loads(message)
                    logger.info(f"Received JSON: {data.keys()}")
                    if data.get('event') == 'mark':
                         logger.info(f"Mark received: {data}")
                    elif data.get('event') == 'clear':
                         logger.info("Clear received")
                except json.JSONDecodeError:
                    # Likely binary audio data
                    logger.info(f"Received binary audio data: {len(message)} bytes")

            except asyncio.TimeoutError:
                logger.info("No more messages received for 5 seconds. Stopping listener.")
                break
            except websockets.exceptions.ConnectionClosed:
                logger.warning("Connection closed by server.")
                break
                
    except Exception as e:
        logger.error(f"Error in receive loop: {e}")

async def run_test():
    logger.info("Phase 1: Dependency & Audio Prep")
    try:
        audio_data = generate_dummy_audio()
        logger.info(f"Generated {len(audio_data)} bytes of audio (PCM16LE, 8kHz, Mono).")
    except Exception as e:
        logger.critical(f"Failed to generate audio: {e}")
        return

    logger.info(f"Phase 2: WebSocket Connection to {WS_URL}")
    try:
        async with websockets.connect(WS_URL) as ws:
            logger.info("Connected to Exotel Bridge.")
            
            # Send initial 'connected' or 'start' event if required by Exotel protocol
            # However, standard Exotel media stream just starts sending media.
            # We will send a start event just in case the bridge expects it, 
            # though the user prompt implies just sending media frames.
            # Let's stick to the prompt: "For each frame, construct a JSON message..."
            # Provide stream_sid just in case
            stream_sid = "test_stream_123"
            
            await ws.send(json.dumps({
                "event": "start",
                "streamSid": stream_sid,
                "start": {
                    "streamSid": stream_sid,
                     "accountSid": "test_account",
                     "callSid": "test_call",
                     "tracks": ["inbound"]
                }
            }))
            logger.info("Sent 'start' event.")

            # Create tasks for sending and receiving
            sender_task = asyncio.create_task(send_audio(ws, audio_data))
            receiver_task = asyncio.create_task(receive_messages(ws))
            
            await sender_task
            
            # Allow some time for responses to come back after finishing sending
            await receiver_task
            
            logger.info("Phase 3: Test Complete")

    except ConnectionRefusedError:
        logger.critical(f"Connection refused at {WS_URL}. Is the bridge running?")
    except Exception as e:
        logger.critical(f"Test failed with exception: {e}")

if __name__ == "__main__":
    asyncio.run(run_test())
