# PersonaPlex × Exotel Bridge Architecture

## System Overview

PersonaPlex is a real-time, full-duplex speech-to-speech conversational AI model that enables persona control through text-based role prompts and audio-based voice conditioning. This document describes the architecture of the PersonaPlex engine and its integration with Exotel via a WebSocket bridge.

## Repository Structure

```
personaplex/
├── moshi/                    # Core PersonaPlex engine (Moshi-based)
│   ├── moshi/
│   │   ├── server.py         # Main HTTP/WS server (port 8998)
│   │   ├── models/           # Model definitions (Mimi, LM)
│   │   ├── modules/          # Neural network modules
│   │   └── utils/            # Utilities (SSL, logging, connection)
│   └── requirements.txt       # Python dependencies
├── exotel_bridge.py          # WebSocket bridge service (port 5050)
├── client/                   # Web UI frontend (React/TypeScript)
├── assets/                   # Test assets and voice prompts
├── Dockerfile               # Container build for PersonaPlex
└── docker-compose.yaml      # Docker Compose configuration
```

## Core Components

### 1. PersonaPlex Engine (`moshi/moshi/server.py`)

**Entry Point:** `python -m moshi.server --ssl <cert_dir>`

**Port:** 8998 (HTTPS/WSS)

**Key Responsibilities:**
- Loads Mimi encoder/decoder models and LM (Language Model)
- Serves WebSocket endpoint at `/api/chat`
- Handles real-time audio streaming (Opus codec)
- Processes voice prompts and text prompts
- Generates conversational speech responses

**WebSocket Endpoint:** `wss://<host>:8998/api/chat?voice_prompt=<file>&text_prompt=<text>`

**Message Protocol (Binary Frames):**
- `0x00`: Handshake/keepalive (server sends after system prompts)
- `0x01`: Audio payload (Opus-encoded bytes)
- `0x02`: Text tokens (UTF-8 encoded)

**Model Specifications:**
- Sample Rate: 24000 Hz (Mimi encoder/decoder)
- Frame Rate: 12.5 Hz
- Audio Format: Opus codec via `sphn` library
- Voice Prompts: `.pt` files containing pre-computed embeddings

**Key Files:**
- `moshi/moshi/server.py:135-309` - WebSocket handler (`handle_chat`)
- `moshi/moshi/server.py:460` - Route registration (`/api/chat`)
- `moshi/moshi/models/loaders.py:39` - Model constants (SAMPLE_RATE=24000)

### 2. Exotel Bridge (`exotel_bridge.py`)

**Entry Point:** `python exotel_bridge.py`

**Port:** 5050 (WebSocket)

**Key Responsibilities:**
- Accepts WebSocket connections from Exotel
- Translates Exotel JSON media frames ↔ PersonaPlex binary frames
- Performs audio format conversion:
  - Exotel: PCM16LE @ 8kHz mono
  - PersonaPlex: Opus @ 24kHz (via PCM intermediate)
- Manages resampling pipelines (ffmpeg subprocesses)
- Handles Opus encoding/decoding (sphn library)

**Exotel Protocol (JSON Text Frames):**
```json
{
  "event": "media",
  "media": {
    "payload": "<base64-encoded PCM16LE 8kHz mono>"
  }
}
```

**Bridge Configuration:**
- `BRIDGE_HOST`: "0.0.0.0"
- `BRIDGE_PORT`: 5050
- `PERSONAPLEX_WS`: "wss://127.0.0.1:8998/api/chat?voice_prompt=...&text_prompt=..."
- `MODEL_SR`: 48000 (⚠️ **BUG**: Should be 24000 per model spec)
- `EXOTEL_SR`: 8000

**Key Functions:**
- `exotel_to_model_pcm()` - Receives Exotel JSON, extracts PCM, resamples 8k→48k
- `model_pcm_to_personaplex_opus()` - Converts PCM to Opus, sends to PersonaPlex
- `personaplex_to_model_pcm()` - Receives Opus from PersonaPlex, decodes to PCM
- `pcm8k_to_exotel_out()` - Resamples PCM 48k→8k, sends JSON to Exotel

**Files:**
- `exotel_bridge.py:106-288` - Main handler and dataflow tasks
- `exotel_bridge.py:41-71` - FFmpeg resampler subprocess management

### 3. Client Web UI (`client/`)

**Technology:** React + TypeScript

**Purpose:** Interactive web interface for testing PersonaPlex

**Protocol:** Uses same WebSocket protocol as bridge (`/api/chat`)

## Data Flow Architecture

### Local Path (No Tunnel)

```
Exotel Call
    ↓ (WebSocket JSON media frames)
Exotel Bridge (0.0.0.0:5050)
    ↓ (PCM 8kHz → 48kHz resample)
    ↓ (PCM → Opus encode)
    ↓ (Binary WS: 0x01 + Opus bytes)
PersonaPlex Engine (127.0.0.1:8998 /api/chat)
    ↓ (Opus decode → PCM 24kHz)
    ↓ (Mimi encode → LM → Mimi decode)
    ↓ (PCM → Opus encode)
    ↓ (Binary WS: 0x01 + Opus bytes)
Exotel Bridge
    ↓ (Opus decode → PCM 48kHz)
    ↓ (PCM 48kHz → 8kHz resample)
    ↓ (PCM → base64 → JSON)
Exotel Call
```

### Tunnel Path (ngrok/cloudflared)

```
Exotel Call
    ↓ (HTTPS/WSS)
ngrok/cloudflared Tunnel (public URL)
    ↓ (HTTPS/WSS)
Exotel Bridge (0.0.0.0:5050)
    ↓ (same as local path)
PersonaPlex Engine (127.0.0.1:8998)
    ↓ (same as local path)
Exotel Bridge
    ↓ (same as local path)
Exotel Call
```

## Audio Format Specifications

| Component | Format | Sample Rate | Channels | Encoding |
|-----------|--------|-------------|----------|----------|
| Exotel | PCM16LE | 8000 Hz | Mono | Raw PCM |
| Bridge (Intermediate) | PCM16LE | 48000 Hz* | Mono | Raw PCM |
| PersonaPlex Input/Output | Opus | 24000 Hz | Mono | Opus (via sphn) |

*⚠️ **Known Issue**: Bridge uses 48000 Hz but model expects 24000 Hz. This causes audio speed issues.

## Dependencies

### PersonaPlex Engine
- Python 3.11+
- PyTorch (CUDA recommended)
- `sphn` (Opus codec)
- `aiohttp` (WebSocket server)
- `huggingface-hub` (model downloads)
- `sentencepiece` (text tokenizer)

### Exotel Bridge
- Python 3.11+
- `websockets` (WebSocket client/server)
- `numpy` (audio processing)
- `sphn` (Opus codec)
- `ffmpeg` (audio resampling) - **system dependency**

## Configuration

### Environment Variables
- `HF_TOKEN`: HuggingFace token for model access
- `BRIDGE_PORT`: Bridge listening port (default: 5050)

### PersonaPlex Server Arguments
- `--host`: Bind address (default: "localhost")
- `--port`: Server port (default: 8998)
- `--ssl`: Directory containing `cert.pem` and `key.pem`
- `--voice-prompt-dir`: Directory with `.pt` voice prompt files
- `--cpu-offload`: Offload LM layers to CPU (for low-memory GPUs)
- `--gradio-tunnel`: Enable Gradio tunnel (alternative to ngrok)

### Bridge Configuration (Hardcoded)
- `BRIDGE_HOST`: "0.0.0.0"
- `BRIDGE_PORT`: 5050
- `VOICE_PROMPT`: "NATF0.pt"
- `TEXT_PROMPT`: "You enjoy having a good conversation."
- `MODEL_SR`: 48000 (should be 24000)
- `EXOTEL_SR`: 8000

## Security Considerations

- PersonaPlex uses self-signed SSL certificates (via `mkcert` or manual)
- Bridge connects to PersonaPlex with SSL verification disabled (`ssl_no_verify()`)
- Bridge listens on `0.0.0.0:5050` (exposed to network)
- No authentication on bridge WebSocket endpoint
- Exotel must connect via tunnel (ngrok/cloudflared) for public access

## Known Issues

1. **Sample Rate Mismatch**: Bridge uses `MODEL_SR = 48000` but PersonaPlex model expects 24000 Hz. This causes audio speed/pitch issues.
2. **No Reconnection Logic**: Bridge does not handle PersonaPlex disconnections gracefully.
3. **No Error Recovery**: FFmpeg subprocess failures are not retried.
4. **Hardcoded Configuration**: Bridge settings are not environment-configurable.
5. **No Health Checks**: No `/health` endpoint for monitoring.
