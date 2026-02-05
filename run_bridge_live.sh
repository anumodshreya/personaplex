#!/bin/bash
# PHASE 3: Run bridge in foreground with live debugging
# Stop all background instances first

echo "Stopping all background exotel_bridge instances..."
pkill -f exotel_bridge.py || true
sleep 1

# Check if any are still running
if pgrep -f exotel_bridge.py > /dev/null; then
    echo "Warning: Some instances still running, force killing..."
    pkill -9 -f exotel_bridge.py || true
    sleep 1
fi

echo "Starting exotel_bridge in foreground mode (unbuffered)..."
echo "Press Ctrl+C to stop"
echo ""

# Run with -u flag for unbuffered output
cd /workspace/personaplex
python -u exotel_bridge.py
