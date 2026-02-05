#!/bin/bash
# Phase 1: Engine Offline Baseline Test
# Generate ground truth WAV from engine offline mode

set -e

cd /workspace/personaplex
source .venv/bin/activate

echo "=== PHASE 1: Engine Offline Baseline ==="

# Check if input wav exists, create a simple tone if not
if [ ! -f "assets/test/input_assistant.wav" ]; then
    echo "Creating test input WAV..."
    mkdir -p assets/test
    # Generate 1 second of 440Hz tone at 8kHz mono
    ffmpeg -hide_banner -loglevel error -y -f lavfi -i "sine=frequency=440:duration=1" -ar 8000 -ac 1 assets/test/input_assistant.wav
fi

echo "Running engine offline mode..."
python -m moshi.offline \
    --voice-prompt NATF0.pt \
    --text-prompt "You enjoy having a good conversation. Say HELLO." \
    --input-wav assets/test/input_assistant.wav \
    --output-wav logs/engine_offline_out.wav \
    --output-text logs/engine_offline_out.json

echo ""
echo "Results:"
ls -lh logs/engine_offline_out.wav
if command -v soxi &> /dev/null; then
    soxi logs/engine_offline_out.wav
else
    ffprobe -hide_banner -i logs/engine_offline_out.wav 2>&1 | head -10
fi

if [ -f logs/engine_offline_out.json ]; then
    echo ""
    echo "Text output:"
    cat logs/engine_offline_out.json
fi
