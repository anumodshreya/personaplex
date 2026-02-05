# PersonaPlex × Exotel Bridge - Cold Start Runbook

This runbook provides step-by-step instructions to start the PersonaPlex engine and Exotel bridge from a cold start.

## Prerequisites

### System Requirements
- Python 3.11+
- CUDA-capable GPU (recommended) or CPU
- FFmpeg installed (`apt install ffmpeg` or `brew install ffmpeg`) - **REQUIRED for bridge**
- Opus development library (`apt install libopus-dev` or `brew install opus`)
- HuggingFace token (for model downloads) - **Set as environment variable, never commit to repo**

### Verify Prerequisites

```bash
# Check Python version
python3 --version  # Should be 3.11+

# Check FFmpeg
ffmpeg -version

# Check Opus library
pkg-config --modversion opus

# Check GPU (if using CUDA)
nvidia-smi
```

## Quick Start (Automated)

### Single Command Setup

```bash
cd /workspace/personaplex
export HF_TOKEN="<your_token>"  # Never commit tokens to repo!
bash scripts/setup_and_run.sh
```

**Important:** Replace `<your_token>` with your actual HuggingFace token. Never commit tokens to the repository.

This script will:
- Install all system dependencies (libopus-dev, ffmpeg, etc.)
- Create and activate Python virtual environment
- Install all Python dependencies
- Start PersonaPlex engine (port 8998)
- Start Exotel bridge (port 5050)
- Print health check commands

## Manual Setup (Alternative)

### Step 1: Environment Setup

### 1.1 Navigate to Project Directory

```bash
cd /workspace/personaplex
```

### 1.2 Set HuggingFace Token

```bash
export HF_TOKEN="<your_token>"  # Replace with your actual token
```

**Security Note:** 
- Never commit tokens to the repository
- Never echo or print token values
- Set token in shell session only or use `.env` file (gitignored)

**Note:** You must accept the PersonaPlex model license at https://huggingface.co/nvidia/personaplex-7b-v1 before using the token.

### 1.3 (Optional) Create Virtual Environment

If not using the existing `.venv`:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 1.4 Install Dependencies

```bash
# Install PersonaPlex package
pip install moshi/.

# Install bridge dependencies
pip install websockets numpy python-dotenv

# Verify installation
python -c "import moshi; print('OK')"
```

## Step 2: Start PersonaPlex Engine

### 2.1 Start Engine Only

```bash
cd /workspace/personaplex
source .venv/bin/activate
export HF_TOKEN="your_token"

SSL_DIR=$(mktemp -d)
python -m moshi.server --ssl "$SSL_DIR" --port 8998 > logs/engine.log 2>&1 &
echo $! > logs/engine.pid
```

### 2.2 Create SSL Certificate Directory (Alternative)

```bash
SSL_DIR=$(mktemp -d)
echo "SSL_DIR=$SSL_DIR"  # Save this for later
```

**Alternative:** Use existing directory:
```bash
SSL_DIR="/app/ssl"  # If using Docker
# or
SSL_DIR="$HOME/.personaplex-ssl"
mkdir -p "$SSL_DIR"
```

### 2.3 Start PersonaPlex Server (Detailed)

**Basic Start (GPU):**
```bash
SSL_DIR=$(mktemp -d)
python -m moshi.server --ssl "$SSL_DIR" --port 8998
```

**With CPU Offload (Low GPU Memory):**
```bash
SSL_DIR=$(mktemp -d)
python -m moshi.server --ssl "$SSL_DIR" --port 8998 --cpu-offload
```

**With Custom Voice Prompt Directory:**
```bash
SSL_DIR=$(mktemp -d)
python -m moshi.server \
  --ssl "$SSL_DIR" \
  --port 8998 \
  --voice-prompt-dir /path/to/voices
```

**With Gradio Tunnel (Alternative to ngrok):**
```bash
SSL_DIR=$(mktemp -d)
python -m moshi.server \
  --ssl "$SSL_DIR" \
  --port 8998 \
  --gradio-tunnel
```

### 2.3 Verify PersonaPlex is Running

**Check Logs for:**
```
Access the Web UI directly at https://<IP>:8998
```

**Test HTTP Endpoint:**
```bash
curl -k https://localhost:8998/ 2>&1 | head -5
# Should return HTML or 200 OK
```

**Test WebSocket Handshake:**
```bash
# Using wscat (install: npm install -g wscat)
wscat -c "wss://localhost:8998/api/chat?voice_prompt=NATF0.pt&text_prompt=test" \
  --no-check

# Or using Python
python3 << 'EOF'
import asyncio
import ssl
import websockets

async def test():
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    async with websockets.connect(
        "wss://localhost:8998/api/chat?voice_prompt=NATF0.pt&text_prompt=test",
        ssl=ssl_ctx
    ) as ws:
        msg = await ws.recv()
        print(f"Received: {msg.hex() if isinstance(msg, bytes) else msg}")

asyncio.run(test())
EOF
```

**Expected:** Receive `0x00` handshake byte.

## Step 3: Start Exotel Bridge

### 3.1 Start Bridge Only

```bash
cd /workspace/personaplex
source .venv/bin/activate

# Optional: Set environment variables (or use defaults)
export ENGINE_URL="wss://127.0.0.1:8998/api/chat"
export BRIDGE_HOST="0.0.0.0"
export BRIDGE_PORT="5050"
export MODEL_SR="24000"
export EXOTEL_SR="8000"
export VOICE_PROMPT="NATF0.pt"
export TEXT_PROMPT="You enjoy having a good conversation."

python exotel_bridge.py > logs/bridge.log 2>&1 &
echo $! > logs/bridge.pid
```

### 3.2 Verify Bridge Configuration

Bridge supports environment variables:
- `ENGINE_URL`: PersonaPlex WebSocket URL (default: `wss://127.0.0.1:8998/api/chat`)
- `BRIDGE_HOST`: Bridge listen address (default: `0.0.0.0`)
- `BRIDGE_PORT`: Bridge listen port (default: `5050`)
- `MODEL_SR`: Model sample rate (default: `24000`)
- `EXOTEL_SR`: Exotel sample rate (default: `8000`)
- `VOICE_PROMPT`: Voice prompt file (default: `NATF0.pt`)
- `TEXT_PROMPT`: Text prompt (default: `You enjoy having a good conversation.`)

### 3.3 Start Bridge Service (Foreground)

```bash
cd /workspace/personaplex
source .venv/bin/activate
python exotel_bridge.py
```

**Expected Output:**
```
[2024-02-05 10:00:00] [INFO] [exotel_bridge] Bridge listening on: ws://0.0.0.0:5050
[2024-02-05 10:00:00] [INFO] [exotel_bridge] Bridge ready! Waiting for Exotel connections...
```

### 3.3 Verify Bridge is Running

**Test WebSocket Connection:**
```bash
# Using wscat
wscat -c "ws://localhost:5050"

# Or using Python
python3 << 'EOF'
import asyncio
import websockets
import json

async def test():
    async with websockets.connect("ws://localhost:5050") as ws:
        # Send test media frame
        test_frame = {
            "event": "media",
            "media": {"payload": "AAAAAA=="}  # Base64 silence
        }
        await ws.send(json.dumps(test_frame))
        print("Sent test frame")

asyncio.run(test())
EOF
```

## Step 4: Set Up Tunnel (For Public Access)

To expose the bridge to Exotel, use a tunnel service. The bridge listens on `0.0.0.0:5050` locally.

### Option A: ngrok

**Install ngrok:**
```bash
# Download from https://ngrok.com/download
# Or via package manager
curl -s https://ngrok-agent.s3.amazonaws.com/ngrok.asc | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null
echo "deb https://ngrok-agent.s3.amazonaws.com buster main" | sudo tee /etc/apt/sources.list.d/ngrok.list
sudo apt update && sudo apt install ngrok
```

**Start Tunnel:**
```bash
ngrok http 5050
```

**Copy the HTTPS URL:**
```
Forwarding: https://xxxx-xx-xx-xx-xx.ngrok.io -> http://localhost:5050
```

**Use this URL (`wss://xxxx-xx-xx-xx-xx.ngrok.io`) in Exotel configuration.**

### Option B: cloudflared

**Install cloudflared:**
```bash
# Download from https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O cloudflared
chmod +x cloudflared
sudo mv cloudflared /usr/local/bin/
```

**Start Tunnel:**
```bash
cloudflared tunnel --url http://localhost:5050
```

**Copy the HTTPS URL from output. Use `wss://` version in Exotel.**

### Option C: Gradio Tunnel (Built-in)

If you started PersonaPlex with `--gradio-tunnel`, it will print a tunnel URL. However, this tunnels the PersonaPlex server (8998), not the bridge (5050). You would need to configure Exotel to connect directly to PersonaPlex, bypassing the bridge.

## Step 5: Configure Exotel

### 5.1 Exotel WebSocket Configuration

In Exotel dashboard, configure:
- **WebSocket URL**: `wss://xxxx-xx-xx-xx-xx.ngrok.io` (or your tunnel URL)
- **Protocol**: WebSocket Secure (WSS)
- **Media Format**: PCM, 8kHz, Mono, 16-bit

### 5.2 Test Exotel Connection

Make a test call through Exotel and verify:
1. Bridge receives connection: `[bridge] exotel client connected`
2. Bridge connects to PersonaPlex: `[bridge] connected to personaplex`
3. Handshake completes: `[bridge] got personaplex handshake`
4. Audio flows in both directions

## Step 6: Health Checks

### 6.1 PersonaPlex Health

```bash
# Check if process is running
ps aux | grep "moshi.server"

# Check port is listening
netstat -tlnp | grep 8998
# or
ss -tlnp | grep 8998

# Test WebSocket endpoint
curl -k -i -N \
  -H "Connection: Upgrade" \
  -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Key: test" \
  -H "Sec-WebSocket-Version: 13" \
  https://localhost:8998/api/chat?voice_prompt=NATF0.pt&text_prompt=test
```

### 6.2 Bridge Health

```bash
# Check if process is running
ps aux | grep "exotel_bridge"

# Check port is listening
netstat -tlnp | grep 5050
# or
ss -tlnp | grep 5050

# Test WebSocket endpoint
curl -i -N \
  -H "Connection: Upgrade" \
  -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Key: test" \
  -H "Sec-WebSocket-Version: 13" \
  http://localhost:5050
```

### 6.3 FFmpeg Processes

```bash
# Check FFmpeg resamplers are running (when bridge has active connection)
ps aux | grep ffmpeg
# Should see 2 processes: pcm8k_to_model and model_to_pcm8k
```

## Step 7: Monitoring and Logs

### 7.1 PersonaPlex Logs

PersonaPlex logs to stdout. To save logs:
```bash
python -m moshi.server --ssl "$SSL_DIR" --port 8998 2>&1 | tee personaplex.log
```

**Key Log Messages:**
- `Access the Web UI directly at https://...` - Server started
- `Incoming connection from ...` - WebSocket connection received
- `sent handshake bytes` - Handshake sent to client

### 7.2 Bridge Logs

Bridge logs to stdout. To save logs:
```bash
python exotel_bridge.py 2>&1 | tee bridge.log
```

**Key Log Messages:**
- `[bridge] listening on 0.0.0.0:5050` - Bridge started
- `[bridge] exotel client connected` - Exotel connection received
- `[bridge] connected to personaplex` - PersonaPlex connection established
- `[bridge] got personaplex handshake` - Handshake received
- `[ffmpeg_*]` - FFmpeg stderr output (errors)

### 7.3 Tail Logs

```bash
# Terminal 1: PersonaPlex
tail -f personaplex.log

# Terminal 2: Bridge
tail -f bridge.log

# Terminal 3: System logs
journalctl -f  # If using systemd
```

## Step 8: Simulated Exotel Request (Testing)

### 8.1 Python Test Script

Create `test_exotel_bridge.py`:

```python
import asyncio
import json
import base64
import websockets

async def test():
    async with websockets.connect("ws://localhost:5050") as ws:
        # Send 100ms of silence (1600 bytes @ 8kHz PCM16LE)
        silence = b"\x00" * 1600
        payload = base64.b64encode(silence).decode("ascii")
        
        frame = {
            "event": "media",
            "media": {"payload": payload}
        }
        
        await ws.send(json.dumps(frame))
        print("Sent test frame")
        
        # Wait for response
        try:
            response = await asyncio.wait_for(ws.recv(), timeout=5.0)
            data = json.loads(response)
            print(f"Received: {data.get('event')}")
        except asyncio.TimeoutError:
            print("No response (timeout)")

asyncio.run(test())
```

**Run:**
```bash
python test_exotel_bridge.py
```

## Quick Start Commands (All-in-One)

### Option 1: Automated Setup (Recommended)

```bash
cd /workspace/personaplex
export HF_TOKEN="your_token"
bash scripts/setup_and_run.sh
```

### Option 2: Manual Start (Terminal 1: PersonaPlex)
```bash
cd /workspace/personaplex
source .venv/bin/activate
export HF_TOKEN="your_token"
SSL_DIR=$(mktemp -d)
python -m moshi.server --ssl "$SSL_DIR" --port 8998 > logs/engine.log 2>&1 &
echo $! > logs/engine.pid
tail -f logs/engine.log
```

### Option 2: Manual Start (Terminal 2: Bridge)
```bash
cd /workspace/personaplex
source .venv/bin/activate
python exotel_bridge.py > logs/bridge.log 2>&1 &
echo $! > logs/bridge.pid
tail -f logs/bridge.log
```

### Option 2: Manual Start (Terminal 3: Tunnel if needed)
```bash
ngrok http 5050
```

## Docker Compose Start

If using Docker:

```bash
cd /workspace/personaplex

# Create .env file
cat > .env << EOF
HF_TOKEN=your_token_here
EOF

# Start services
docker-compose up -d

# View logs
docker-compose logs -f personaplex
```

**Note:** Bridge is not included in docker-compose.yaml. You need to run it separately or add it to the compose file.

## Troubleshooting

See `TROUBLESHOOTING.md` for common issues and solutions.

## Verification Checklist

- [ ] PersonaPlex server starts without errors
- [ ] PersonaPlex WebSocket endpoint responds to handshake
- [ ] Bridge starts and listens on port 5050
- [ ] Bridge can connect to PersonaPlex (check logs)
- [ ] Tunnel is running (if using public access)
- [ ] Exotel can connect to bridge/tunnel
- [ ] Audio flows in both directions during test call
- [ ] No FFmpeg errors in bridge logs
- [ ] No WebSocket connection errors
