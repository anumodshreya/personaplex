# PersonaPlex × Exotel Bridge - Troubleshooting Guide

## Security Notes

### Token Management
- **NEVER commit HF_TOKEN to repository**: Always set it as an environment variable
- **Rotate tokens**: If a token is leaked, revoke it at https://huggingface.co/settings/tokens and create a new one
- **Scrub shell history**: If token was accidentally entered in terminal:
  ```bash
  # Clear bash history (be careful!)
  history -c
  # Or edit ~/.bash_history and remove the line
  ```

### Environment Variables
- Set `HF_TOKEN` in your shell session only: `export HF_TOKEN="<token>"`
- Use `.env` files (gitignored) for local development
- Never echo or print token values in scripts

## Common Issues and Solutions

### Issue 0: Missing libopus-dev

**Symptoms:**
- Error during `pip install moshi/.`
- "fatal error: opus/opus.h: No such file or directory"
- Build fails with opus-related errors

**Checks:**
```bash
# Check if libopus-dev is installed
dpkg -l | grep libopus-dev
pkg-config --modversion opus
```

**Solutions:**
1. **Install libopus-dev:**
   ```bash
   sudo apt-get update
   sudo apt-get install -y libopus-dev
   ```
2. **Reinstall PersonaPlex:**
   ```bash
   pip install moshi/. --force-reinstall --no-cache-dir
   ```

---

### Issue 1: PersonaPlex Server Won't Start

**Symptoms:**
- Server exits immediately after start
- Error: "Cannot find model" or "HF_TOKEN not set"
- Port 8998 already in use

**Checks:**
```bash
# Check if port is in use
netstat -tlnp | grep 8998
# or
lsof -i :8998

# Check HF_TOKEN
echo $HF_TOKEN

# Check model download
ls -la ~/.cache/huggingface/hub/models--nvidia--personaplex-7b-v1/
```

**Solutions:**
1. **Port conflict:** Kill process using port 8998 or use `--port <other_port>`
2. **Missing HF_TOKEN:** `export HF_TOKEN="your_token"`
3. **Model not downloaded:** Server will download automatically on first run (requires HF_TOKEN)
4. **SSL cert issues:** Check `--ssl` directory has `cert.pem` and `key.pem`, or let server auto-generate

**Code Reference:** `moshi/moshi/server.py:357-479` - Server startup

---

### Issue 1.5: FFmpeg Not Found

**Symptoms:**
- Bridge starts but FFmpeg processes fail immediately
- "ffmpeg_* exited immediately" errors
- "ffmpeg: command not found"

**Checks:**
```bash
# Check if ffmpeg is installed
which ffmpeg
ffmpeg -version
```

**Solutions:**
1. **Install FFmpeg:**
   ```bash
   sudo apt-get update
   sudo apt-get install -y ffmpeg
   ```
2. **Verify installation:**
   ```bash
   ffmpeg -version
   ```

---

### Issue 2: Bridge Cannot Connect to PersonaPlex

**Symptoms:**
- Connection retry messages in logs
- Error: "Connection refused" or "SSL verification failed"
- Timeout errors
- "Failed to connect to PersonaPlex after 5 attempts"

**Checks:**
```bash
# Verify PersonaPlex is running
curl -k https://localhost:8998/ 2>&1 | head -1

# Check if PersonaPlex process is running
ps aux | grep "moshi.server"

# Test WebSocket connection manually
python3 << 'EOF'
import asyncio
import ssl
import websockets

async def test():
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    try:
        async with websockets.connect(
            "wss://127.0.0.1:8998/api/chat?voice_prompt=NATF0.pt&text_prompt=test",
            ssl=ssl_ctx,
            timeout=5
        ) as ws:
            print("Connected!")
            msg = await ws.recv()
            print(f"Received: {msg.hex()}")
    except Exception as e:
        print(f"Error: {e}")

asyncio.run(test())
EOF
```

**Solutions:**
1. **Wrong host:** Check `ENGINE_URL` environment variable. Default is `wss://127.0.0.1:8998/api/chat`. If PersonaPlex is on a different host, set `export ENGINE_URL="wss://<host>:8998/api/chat"`
2. **SSL issues:** Bridge disables SSL verification (`ssl_no_verify()`), but PersonaPlex must serve HTTPS. Ensure PersonaPlex started with `--ssl` flag
3. **Port mismatch:** Verify PersonaPlex is on port 8998 (check logs or `netstat -tlnp | grep 8998`)
4. **PersonaPlex not started:** Start PersonaPlex first, then bridge. Bridge will retry 5 times with exponential backoff
5. **Wrong scheme (ws vs wss):** PersonaPlex uses `wss://` (secure WebSocket). Bridge expects `ENGINE_URL` to start with `wss://`

**Code Reference:** `exotel_bridge.py:140-160` - PersonaPlex connection with retry logic

---

### Issue 3: Handshake Timeout

**Symptoms:**
- `[bridge] got personaplex handshake` never appears
- Error: "Did not receive PersonaPlex handshake (0x00)"
- Bridge waits indefinitely

**Checks:**
```bash
# Check PersonaPlex logs for "sent handshake bytes"
grep "handshake" personaplex.log

# Verify voice prompt file exists
ls -la /path/to/voices/NATF0.pt
```

**Solutions:**
1. **Voice prompt missing:** Ensure voice prompt file exists in `--voice-prompt-dir`
2. **System prompts taking too long:** Increase timeout in `wait_for_handshake()` (default 15s)
3. **WebSocket connection broken:** Check PersonaPlex logs for errors

**Code Reference:**
- `exotel_bridge.py:119-130` - Handshake wait logic
- `moshi/moshi/server.py:286-288` - Handshake send

---

### Issue 4: Audio Speed/Pitch Issues

**Symptoms:**
- Audio plays too fast or too slow
- Voice sounds high-pitched or low-pitched
- Audio is garbled

**Root Cause:**
Bridge must use `MODEL_SR = 24000` to match PersonaPlex model. This is now fixed by default.

**Checks:**
```bash
# Verify model sample rate
grep "SAMPLE_RATE" /workspace/personaplex/moshi/moshi/models/loaders.py
# Should show: SAMPLE_RATE = 24000

# Check bridge configuration (should be 24000)
grep "MODEL_SR" /workspace/personaplex/exotel_bridge.py
# Should show: MODEL_SR = int(os.getenv("MODEL_SR", "24000"))
```

**Solutions:**
1. **Verify environment variable:** Ensure `MODEL_SR=24000` (default, but can be overridden)
   ```bash
   export MODEL_SR=24000
   ```
2. **Restart bridge** after change
3. **Check logs:** Verify bridge logs show "Audio settings: Exotel=8000Hz, Model=24000Hz"

**Code Reference:**
- `exotel_bridge.py:51` - MODEL_SR definition (now defaults to 24000)
- `moshi/moshi/models/loaders.py:39` - Actual model sample rate (24000)

---

### Issue 5: FFmpeg Process Errors

**Symptoms:**
- `[ffmpeg_*]` errors in bridge logs
- "ffmpeg_8k_to_model exited immediately"
- Audio stops flowing

**Checks:**
```bash
# Check FFmpeg is installed
ffmpeg -version

# Check FFmpeg processes
ps aux | grep ffmpeg

# Check bridge logs for FFmpeg stderr
grep "ffmpeg" bridge.log
```

**Solutions:**
1. **FFmpeg not installed:** `apt install ffmpeg` or `brew install ffmpeg`
2. **FFmpeg process died:** Check stderr output in bridge logs
3. **Pipe errors:** Ensure FFmpeg processes are started before use

**Code Reference:** `exotel_bridge.py:41-71` - FFmpeg resampler setup

---

### Issue 6: Exotel Cannot Connect to Bridge

**Symptoms:**
- Exotel shows "Connection failed"
- Bridge never logs "exotel client connected"
- Timeout from Exotel side

**Checks:**
```bash
# Verify bridge is listening on 0.0.0.0:5050
netstat -tlnp | grep 5050
# Should show: 0.0.0.0:5050

# Test local connection
wscat -c "ws://localhost:5050"

# Test from remote (if tunnel is set up)
wscat -c "wss://your-tunnel-url"
```

**Solutions:**
1. **Bridge not started:** Start bridge service
2. **Firewall blocking:** Check firewall rules for port 5050
3. **Wrong URL in Exotel:** Use tunnel URL (wss://) not localhost
4. **Tunnel not running:** Start ngrok/cloudflared tunnel

**Code Reference:** `exotel_bridge.py:291-294` - Bridge server setup

---

### Issue 7: WebSocket Connection Drops

**Symptoms:**
- Connection works initially, then drops
- "Connection closed" errors
- Audio stops mid-call

**Checks:**
```bash
# Check for keepalive messages
grep "keepalive" bridge.log

# Check PersonaPlex connection status
grep "connection closed" personaplex.log

# Check system resources
nvidia-smi  # GPU memory
free -h     # RAM
```

**Solutions:**
1. **Keepalive not working:** Bridge sends `0x00` every 2s - check if PersonaPlex responds
2. **Network timeout:** Increase WebSocket timeout settings
3. **Resource exhaustion:** Check GPU/RAM usage, use `--cpu-offload` if needed
4. **Tunnel timeout:** Some tunnels (ngrok free tier) have connection limits

**Code Reference:** `exotel_bridge.py:132-140` - Keepalive mechanism

---

### Issue 8: Audio Format Mismatch

**Symptoms:**
- No audio output
- Garbled audio
- "Invalid audio format" errors

**Checks:**
```bash
# Verify Exotel sends correct format
# Should be: PCM16LE, 8kHz, Mono

# Check bridge resampling
# Should resample: 8k → 48k (inbound), 48k → 8k (outbound)
```

**Solutions:**
1. **Exotel format wrong:** Configure Exotel to send PCM16LE @ 8kHz mono
2. **Resampler misconfiguration:** Check FFmpeg command in `start_pcm_resampler()`
3. **Opus codec issues:** Verify `sphn` library is installed correctly

**Code Reference:**
- `exotel_bridge.py:41-71` - Resampler setup
- `exotel_bridge.py:156-157` - Opus codec setup

---

### Issue 9: Voice Prompt Not Found

**Symptoms:**
- Error: "Requested voice prompt 'NATF0.pt' not found"
- PersonaPlex fails to start connection

**Checks:**
```bash
# Check voice prompt directory
ls -la /path/to/voices/

# Check if file exists
find /workspace/personaplex -name "NATF0.pt"

# Check PersonaPlex voice prompt dir setting
grep "voice-prompt-dir" personaplex.log
```

**Solutions:**
1. **File missing:** Download voices from HuggingFace or use `--voice-prompt-dir`
2. **Wrong path:** Verify `--voice-prompt-dir` points to correct directory
3. **Wrong filename:** Check bridge `VOICE_PROMPT` matches actual file name

**Code Reference:**
- `exotel_bridge.py:16` - VOICE_PROMPT setting
- `moshi/moshi/server.py:148-162` - Voice prompt loading

---

### Issue 10: High Latency

**Symptoms:**
- Long delay between speech and response
- Audio chunks arrive slowly

**Checks:**
```bash
# Check GPU utilization
nvidia-smi -l 1

# Check CPU usage
top

# Check network latency (if using tunnel)
ping your-tunnel-url
```

**Solutions:**
1. **GPU not used:** Ensure CUDA is available, use GPU not CPU
2. **Model loading:** First call is slower (model warmup)
3. **Network latency:** Tunnel adds latency, use local connection if possible
4. **CPU offload:** If using `--cpu-offload`, latency increases

---

### Issue 11: Memory Issues (OOM)

**Symptoms:**
- "Out of memory" errors
- Process killed by system
- CUDA OOM errors

**Checks:**
```bash
# Check GPU memory
nvidia-smi

# Check system RAM
free -h

# Check process memory
ps aux | grep "moshi.server"
```

**Solutions:**
1. **Use CPU offload:** Start PersonaPlex with `--cpu-offload`
2. **Reduce batch size:** Not configurable in current code
3. **Use smaller model:** Not available (only one model)
4. **Increase GPU memory:** Use GPU with more VRAM

**Code Reference:** `moshi/moshi/server.py:373-375` - CPU offload option

---

### Issue 11.5: No Audio Returned (Roundtrip Failure)

**Symptoms:**
- Bridge connects to engine successfully
- Exotel sends media frames
- No audio frames received from engine
- Roundtrip health check fails
- Bridge logs show: "ffmpeg not found"

**Checks:**
```bash
# Verify ffmpeg is installed
ffmpeg -version

# Check bridge logs for errors
tail -f logs/bridge.log | grep -E "error|Error|ERROR|ffmpeg|resampler"

# Verify engine is sending audio
tail -f logs/engine.log | grep -E "opus|audio|frame"

# Test engine directly
python scripts/healthcheck_engine_ws.py
```

**Solutions:**
1. **FFmpeg not installed (MOST COMMON):**
   ```bash
   sudo apt-get install -y ffmpeg
   # Restart bridge after installation
   kill $(cat logs/bridge.pid)
   python exotel_bridge.py > logs/bridge.log 2>&1 &
   echo $! > logs/bridge.pid
   ```

2. **Sample rate mismatch:**
   - Verify `MODEL_SR=24000` in bridge (default)
   - Check engine uses 24000Hz: `grep "SAMPLE_RATE" moshi/moshi/models/loaders.py`

3. **Opus encode/decode errors:**
   - Check bridge logs for Opus-related errors
   - Verify `sphn` library is installed: `pip list | grep sphn`
   - Engine uses `sphn.OpusStreamWriter/Reader` - bridge must match

4. **WebSocket framing errors:**
   - Engine expects: `0x01` + Opus bytes for audio
   - Engine sends: `0x01` + Opus bytes for audio
   - Check bridge logs for "unknown message kind" warnings

5. **WS vs WSS errors:**
   - Engine uses `wss://` (secure WebSocket)
   - Bridge must connect with SSL: `ENGINE_URL="wss://localhost:8998/api/chat"`
   - Bridge disables SSL verification (acceptable for localhost)

6. **Engine not generating audio:**
   - Check engine logs for model loading errors
   - Verify GPU/CPU resources are sufficient
   - Test engine WebSocket directly to confirm it sends audio

**Code Reference:**
- `exotel_bridge.py:engine_to_exotel()` - Outbound transcoding
- `exotel_bridge.py:exotel_to_engine()` - Inbound transcoding
- `moshi/moshi/server.py:195-199` - Engine frame format

---

### Issue 11.6: HF_TOKEN Missing / License Not Accepted

**Symptoms:**
- "HF_TOKEN environment variable is not set!"
- "401 Unauthorized" when downloading models
- "Repository not found" or "Access denied"

**Checks:**
```bash
# Check if HF_TOKEN is set
echo $HF_TOKEN

# Test token validity
python3 << 'EOF'
import os
from huggingface_hub import HfApi
token = os.getenv("HF_TOKEN")
if not token:
    print("HF_TOKEN not set")
else:
    api = HfApi(token=token)
    try:
        api.model_info("nvidia/personaplex-7b-v1")
        print("Token is valid")
    except Exception as e:
        print(f"Token error: {e}")
EOF
```

**Solutions:**
1. **Set HF_TOKEN:**
   ```bash
   export HF_TOKEN="your_huggingface_token_here"
   ```
2. **Accept model license:**
   - Visit https://huggingface.co/nvidia/personaplex-7b-v1
   - Log in with your HuggingFace account
   - Click "Accept" on the license agreement
3. **Generate token:**
   - Go to https://huggingface.co/settings/tokens
   - Create a new token with "read" access
   - Copy and use it as `HF_TOKEN`

---

### Issue 12: SSL Certificate Errors

**Symptoms:**
- "SSL verification failed"
- "Certificate verify failed"
- Cannot connect via WSS

**Checks:**
```bash
# Check certificate files exist
ls -la $SSL_DIR/cert.pem $SSL_DIR/key.pem

# Test certificate
openssl x509 -in $SSL_DIR/cert.pem -text -noout
```

**Solutions:**
1. **Auto-generate certs:** Let PersonaPlex create certs automatically (uses mkcert)
2. **Manual certs:** Place `cert.pem` and `key.pem` in SSL directory
3. **Bridge SSL verify:** Bridge disables verification, but PersonaPlex must serve HTTPS

**Code Reference:**
- `moshi/moshi/utils/connection.py:202-228` - SSL context creation
- `exotel_bridge.py:34-38` - SSL no-verify setup

---

## Diagnostic Commands

### Check All Services

```bash
# PersonaPlex
curl -k https://localhost:8998/ 2>&1 | head -1 && echo "PersonaPlex: OK" || echo "PersonaPlex: FAIL"

# Bridge
curl -s http://localhost:5050 2>&1 | head -1 && echo "Bridge: OK" || echo "Bridge: FAIL"

# FFmpeg
ffmpeg -version > /dev/null 2>&1 && echo "FFmpeg: OK" || echo "FFmpeg: FAIL"

# GPU
nvidia-smi > /dev/null 2>&1 && echo "GPU: OK" || echo "GPU: FAIL"
```

### Check Ports

```bash
netstat -tlnp | grep -E "8998|5050"
```

### Check Processes

```bash
ps aux | grep -E "moshi.server|exotel_bridge|ffmpeg" | grep -v grep
```

### Check Logs for Errors

```bash
# PersonaPlex errors
grep -i error personaplex.log | tail -10

# Bridge errors
grep -i error bridge.log | tail -10

# FFmpeg errors
grep "ffmpeg" bridge.log | grep -i error | tail -10
```

## Getting Help

1. **Check logs first:** Both PersonaPlex and Bridge log to stdout
2. **Verify configuration:** Ensure all settings match expected values
3. **Test components individually:** Start PersonaPlex, test it, then add bridge
4. **Check known issues:** Review this document and `TODO_NEXT.md` for known bugs

## Common Error Messages

| Error Message | Cause | Solution |
|--------------|-------|----------|
| "Connection refused" | PersonaPlex not running | Start PersonaPlex server |
| "Did not receive PersonaPlex handshake" | Voice prompt missing or timeout | Check voice prompt file, increase timeout |
| "ffmpeg_* exited immediately" | FFmpeg not installed or command error | Install FFmpeg, check command syntax |
| "Requested voice prompt not found" | Voice prompt file missing | Download voices or set correct path |
| "Out of memory" | GPU/RAM insufficient | Use `--cpu-offload` or increase resources |
| "SSL verification failed" | Certificate issues | Let server auto-generate certs |
