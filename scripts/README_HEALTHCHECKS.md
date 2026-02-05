# Health Check Scripts

## Prerequisites

Before running health checks, ensure:
1. `HF_TOKEN` environment variable is set
2. PersonaPlex engine is running on port 8998
3. Exotel bridge is running on port 5050

## Usage

### Engine Health Check

```bash
cd /workspace/personaplex
source .venv/bin/activate
python scripts/healthcheck_engine_ws.py
```

Expected output:
- `✓ CONNECTED to PersonaPlex engine`
- `✓ Received handshake: 00 (type: 0)`
- `✓ Engine WebSocket health check PASSED`

### Bridge Health Check

```bash
cd /workspace/personaplex
source .venv/bin/activate
python scripts/healthcheck_bridge_ws.py
```

Expected output:
- `✓ CONNECTED to Exotel bridge`
- `✓ Sent start event`
- `✓ Sent media event`
- `✓ Bridge WebSocket health check PASSED`

## Troubleshooting

If engine health check fails:
- Verify engine is running: `ss -lntp | grep 8998`
- Check engine logs: `tail -f logs/engine.log`
- Ensure `HF_TOKEN` is set and valid

If bridge health check fails:
- Verify bridge is running: `ss -lntp | grep 5050`
- Check bridge logs: `tail -f logs/bridge.log`
- Ensure engine is running first
