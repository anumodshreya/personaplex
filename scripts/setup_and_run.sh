#!/bin/bash
set -e

# PersonaPlex × Exotel Bridge - Cold Start Setup Script
# This script installs all dependencies and starts both services

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

echo "=========================================="
echo "PersonaPlex × Exotel Bridge Setup"
echo "=========================================="
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Step 1: System dependencies
echo "[1/8] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y \
    libopus-dev \
    ffmpeg \
    python3-venv \
    build-essential \
    pkg-config \
    curl \
    > /dev/null 2>&1

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓${NC} System dependencies installed"
else
    echo -e "${RED}✗${NC} Failed to install system dependencies"
    exit 1
fi

# Step 2: Create virtual environment
echo "[2/8] Creating Python virtual environment..."
if [ -d ".venv" ]; then
    echo -e "${YELLOW}⚠${NC} .venv already exists, skipping creation"
else
    python3 -m venv .venv
    echo -e "${GREEN}✓${NC} Virtual environment created"
fi

# Step 3: Activate venv and upgrade pip
echo "[3/8] Activating virtual environment and upgrading pip..."
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel -q

# Step 4: Install Python dependencies
echo "[4/8] Installing Python dependencies..."

# Install PersonaPlex package
echo "  → Installing PersonaPlex (moshi)..."
pip install -e moshi/. -q || pip install moshi/. -q

# Install bridge dependencies
echo "  → Installing bridge dependencies..."
pip install websockets numpy python-dotenv -q

# Install from requirements.txt if it exists
if [ -f "moshi/requirements.txt" ]; then
    echo "  → Installing from moshi/requirements.txt..."
    pip install -r moshi/requirements.txt -q
fi

echo -e "${GREEN}✓${NC} Python dependencies installed"

# Step 5: Verify HuggingFace token
echo "[5/8] Checking HuggingFace token..."
if [ -z "$HF_TOKEN" ]; then
    echo -e "${RED}✗${NC} HF_TOKEN environment variable is not set!"
    echo ""
    echo "Please set it before running (NEVER commit tokens to repo):"
    echo "  export HF_TOKEN='<your_token>'"
    echo ""
    echo "You must accept the PersonaPlex model license at:"
    echo "  https://huggingface.co/nvidia/personaplex-7b-v1"
    echo ""
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
else
    # Only check presence, never echo the value
    echo -e "${GREEN}✓${NC} HF_TOKEN is set (length: ${#HF_TOKEN} chars)"
fi

# Step 6: Create directories
echo "[6/8] Creating log and SSL directories..."
mkdir -p logs
SSL_DIR=$(mktemp -d)
echo -e "${GREEN}✓${NC} Directories created (SSL_DIR: $SSL_DIR)"

# Step 7: Start PersonaPlex engine
echo "[7/8] Starting PersonaPlex engine..."
echo "  → Logs: logs/engine.log"
echo "  → Port: 8998"
echo "  → WebSocket: wss://localhost:8998/api/chat"

# Start engine in background
python -m moshi.server \
    --ssl "$SSL_DIR" \
    --port 8998 \
    > logs/engine.log 2>&1 &

ENGINE_PID=$!
echo $ENGINE_PID > logs/engine.pid

# Wait a bit for engine to start
sleep 5

# Check if engine is still running
if ps -p $ENGINE_PID > /dev/null; then
    echo -e "${GREEN}✓${NC} PersonaPlex engine started (PID: $ENGINE_PID)"
else
    echo -e "${RED}✗${NC} PersonaPlex engine failed to start. Check logs/engine.log"
    exit 1
fi

# Step 8: Start Exotel bridge
echo "[8/8] Starting Exotel bridge..."
echo "  → Logs: logs/bridge.log"
echo "  → Port: 5050"
echo "  → WebSocket: ws://0.0.0.0:5050"

# Set default environment variables if not set
export ENGINE_URL="${ENGINE_URL:-wss://127.0.0.1:8998/api/chat}"
export BRIDGE_HOST="${BRIDGE_HOST:-0.0.0.0}"
export BRIDGE_PORT="${BRIDGE_PORT:-5050}"
export MODEL_SR="${MODEL_SR:-24000}"
export EXOTEL_SR="${EXOTEL_SR:-8000}"
export VOICE_PROMPT="${VOICE_PROMPT:-NATF0.pt}"
export TEXT_PROMPT="${TEXT_PROMPT:-You enjoy having a good conversation.}"

# Start bridge in background
python exotel_bridge.py > logs/bridge.log 2>&1 &

BRIDGE_PID=$!
echo $BRIDGE_PID > logs/bridge.pid

# Wait a bit for bridge to start
sleep 2

# Check if bridge is still running
if ps -p $BRIDGE_PID > /dev/null; then
    echo -e "${GREEN}✓${NC} Exotel bridge started (PID: $BRIDGE_PID)"
else
    echo -e "${RED}✗${NC} Exotel bridge failed to start. Check logs/bridge.log"
    kill $ENGINE_PID 2>/dev/null || true
    exit 1
fi

echo ""
echo "=========================================="
echo -e "${GREEN}Setup Complete!${NC}"
echo "=========================================="
echo ""
echo "Services running:"
echo "  • PersonaPlex Engine: PID $ENGINE_PID (logs/engine.log)"
echo "  • Exotel Bridge:     PID $BRIDGE_PID (logs/bridge.log)"
echo ""
echo "Environment variables:"
echo "  ENGINE_URL=$ENGINE_URL"
echo "  BRIDGE_HOST=$BRIDGE_HOST"
echo "  BRIDGE_PORT=$BRIDGE_PORT"
echo "  MODEL_SR=$MODEL_SR"
echo "  EXOTEL_SR=$EXOTEL_SR"
echo ""
echo "=========================================="
echo "Health Checks"
echo "=========================================="
echo ""
echo "1. Check PersonaPlex engine:"
echo "   curl -k https://localhost:8998/ 2>&1 | head -1"
echo ""
echo "2. Check bridge WebSocket (using wscat if installed):"
echo "   wscat -c ws://localhost:5050"
echo ""
echo "3. Test WebSocket connection to PersonaPlex:"
echo "   python3 << 'EOF'"
echo "   import asyncio, ssl, websockets"
echo "   async def test():"
echo "       ssl_ctx = ssl.create_default_context()"
echo "       ssl_ctx.check_hostname = False"
echo "       ssl_ctx.verify_mode = ssl.CERT_NONE"
echo "       async with websockets.connect("
echo "           'wss://localhost:8998/api/chat?voice_prompt=NATF0.pt&text_prompt=test',"
echo "           ssl=ssl_ctx"
echo "       ) as ws:"
echo "           msg = await ws.recv()"
echo "           print(f'Received: {msg.hex()}')"
echo "   asyncio.run(test())"
echo "   EOF"
echo ""
echo "4. View logs:"
echo "   tail -f logs/engine.log"
echo "   tail -f logs/bridge.log"
echo ""
echo "5. Stop services:"
echo "   kill \$(cat logs/engine.pid) \$(cat logs/bridge.pid)"
echo ""
echo "=========================================="
