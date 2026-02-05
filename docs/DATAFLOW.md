# PersonaPlex × Exotel Bridge Data Flow

## End-to-End Data Flow Diagrams

### Local Path (No Tunnel)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          EXOTEL CALL                                    │
│  (Telephony Network)                                                     │
└────────────────────┬────────────────────────────────────────────────────┘
                     │
                     │ WebSocket (WSS)
                     │ JSON Text Frames:
                     │ {
                     │   "event": "media",
                     │   "media": {"payload": "<base64 PCM8k>"}
                     │ }
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    EXOTEL BRIDGE                                        │
│  Port: 5050 (0.0.0.0:5050)                                             │
│  Protocol: WebSocket (text frames)                                      │
│                                                                          │
│  Handler: exotel_bridge.py:handler()                                    │
└────────────────────┬────────────────────────────────────────────────────┘
                     │
                     │ 1. Parse JSON, extract base64 payload
                     │ 2. base64.decode() → PCM16LE 8kHz mono (1600 bytes/chunk)
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│              FFmpeg Resampler (8kHz → 48kHz*)                           │
│  Process: pcm8k_to_model                                               │
│  Command: ffmpeg -f s16le -ar 8000 -ac 1 -i pipe:0                     │
│           -f s16le -ar 48000 -ac 1 pipe:1                              │
└────────────────────┬────────────────────────────────────────────────────┘
                     │
                     │ PCM16LE 48kHz mono (9600 bytes/chunk)
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│              Opus Encoder (sphn.OpusStreamWriter)                       │
│  Input: PCM float32 @ 48kHz                                             │
│  Output: Opus bytes (variable length)                                  │
└────────────────────┬────────────────────────────────────────────────────┘
                     │
                     │ Binary Frame: 0x01 + Opus bytes
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│              PersonaPlex Engine                                         │
│  Endpoint: wss://127.0.0.1:8998/api/chat                               │
│  Query Params:                                                          │
│    - voice_prompt=NATF0.pt                                              │
│    - text_prompt=You enjoy having a good conversation.                  │
│                                                                          │
│  Protocol: Binary WebSocket Frames                                      │
│    - 0x00: Handshake/keepalive                                         │
│    - 0x01: Audio (Opus payload)                                         │
│    - 0x02: Text tokens                                                  │
│                                                                          │
│  Processing Pipeline:                                                   │
│    1. Opus decode → PCM float32 @ 24kHz                                │
│    2. Mimi encode → latent codes                                        │
│    3. LM generation → token sequences                                  │
│    4. Mimi decode → PCM float32 @ 24kHz                                │
│    5. Opus encode → Opus bytes                                         │
└────────────────────┬────────────────────────────────────────────────────┘
                     │
                     │ Binary Frame: 0x01 + Opus bytes
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│              Opus Decoder (sphn.OpusStreamReader)                       │
│  Input: Opus bytes                                                      │
│  Output: PCM float32 @ 48kHz*                                           │
└────────────────────┬────────────────────────────────────────────────────┘
                     │
                     │ PCM float32 → int16 conversion
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│              FFmpeg Resampler (48kHz* → 8kHz)                          │
│  Process: model_to_pcm8k                                                │
│  Command: ffmpeg -f s16le -ar 48000 -ac 1 -i pipe:0                   │
│           -f s16le -ar 8000 -ac 1 pipe:1                               │
└────────────────────┬────────────────────────────────────────────────────┘
                     │
                     │ PCM16LE 8kHz mono (1600 bytes/chunk)
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    EXOTEL BRIDGE                                       │
│  Format: base64.encode(PCM) → JSON                                      │
└────────────────────┬────────────────────────────────────────────────────┘
                     │
                     │ WebSocket (WSS)
                     │ JSON Text Frames:
                     │ {
                     │   "event": "media",
                     │   "media": {"payload": "<base64 PCM8k>"}
                     │ }
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                          EXOTEL CALL                                    │
│  (Telephony Network → Caller hears audio)                              │
└─────────────────────────────────────────────────────────────────────────┘
```

*⚠️ **Known Issue**: Bridge uses 48kHz but model expects 24kHz. This is a bug.

### Tunnel Path (ngrok/cloudflared)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          EXOTEL CALL                                    │
│  (Telephony Network)                                                     │
└────────────────────┬────────────────────────────────────────────────────┘
                     │
                     │ HTTPS/WSS
                     │ Public URL: https://xxxx-xx-xx-xx-xx.ngrok.io:443
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    TUNNEL SERVICE                                       │
│  Options:                                                               │
│    - ngrok: ngrok http 5050                                             │
│    - cloudflared: cloudflared tunnel --url http://localhost:5050         │
│    - gradio: --gradio-tunnel (built-in)                                 │
└────────────────────┬────────────────────────────────────────────────────┘
                     │
                     │ HTTP/WSS (tunneled)
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    EXOTEL BRIDGE                                        │
│  Port: 5050 (0.0.0.0:5050)                                             │
│  (Same processing as local path)                                        │
└────────────────────┬────────────────────────────────────────────────────┘
                     │
                     │ (Continue with same flow as local path)
                     │
                     ▼
┌─────────────────────────────────────────────────────────────────────────┐
│              PersonaPlex Engine                                         │
│  (Same as local path)                                                   │
└─────────────────────────────────────────────────────────────────────────┘
```

## Message Format Details

### Exotel → Bridge (Ingress)

**Protocol:** WebSocket (text frames)

**Frame Format:**
```json
{
  "event": "media",
  "media": {
    "payload": "base64-encoded-PCM16LE-8kHz-mono-bytes"
  }
}
```

**Audio Specs:**
- Format: PCM16LE (signed 16-bit little-endian)
- Sample Rate: 8000 Hz
- Channels: Mono (1 channel)
- Chunk Size: ~1600 bytes (100ms @ 8kHz)

**Code Reference:** `exotel_bridge.py:167-186` - `exotel_to_model_pcm()`

### Bridge → PersonaPlex (Egress)

**Protocol:** WebSocket Secure (binary frames)

**Endpoint:** `wss://127.0.0.1:8998/api/chat?voice_prompt=NATF0.pt&text_prompt=You+enjoy+having+a+good+conversation.`

**Frame Format:**
- `0x00`: Handshake/keepalive (1 byte)
- `0x01`: Audio payload (1 byte prefix + Opus bytes)
- `0x02`: Text tokens (1 byte prefix + UTF-8 bytes)

**Audio Specs:**
- Format: Opus codec (via `sphn.OpusStreamWriter`)
- Sample Rate: 24000 Hz (model native)
- Channels: Mono
- Frame Size: 960 samples (20ms @ 48kHz → resampled to 24kHz internally)

**Code Reference:**
- `exotel_bridge.py:188-209` - `model_pcm_to_personaplex_opus()`
- `moshi/moshi/server.py:195-199` - PersonaPlex audio frame handler

### PersonaPlex → Bridge (Ingress)

**Protocol:** WebSocket Secure (binary frames)

**Frame Format:** Same as Bridge → PersonaPlex

**Handshake Sequence:**
1. Client connects to PersonaPlex
2. PersonaPlex processes system prompts (voice + text)
3. PersonaPlex sends `0x00` handshake byte
4. Bridge waits for `0x00` before sending audio

**Code Reference:**
- `exotel_bridge.py:119-130` - `wait_for_handshake()`
- `moshi/moshi/server.py:286-288` - Handshake send

### Bridge → Exotel (Egress)

**Protocol:** WebSocket (text frames)

**Frame Format:** Same as Exotel → Bridge

**Code Reference:** `exotel_bridge.py:249-264` - `pcm8k_to_exotel_out()`

## Audio Processing Pipeline Details

### Inbound (Exotel → PersonaPlex)

```
Exotel PCM8k (1600 bytes)
    ↓ [base64 decode]
Raw PCM16LE 8kHz mono
    ↓ [FFmpeg resample: 8k → 48k]
Raw PCM16LE 48kHz mono (9600 bytes)
    ↓ [int16 → float32, normalize to [-1, 1]]
PCM float32 48kHz
    ↓ [sphn.OpusStreamWriter.append_pcm()]
Opus bytes (variable length)
    ↓ [Frame: 0x01 + Opus bytes]
PersonaPlex WebSocket
    ↓ [PersonaPlex: Opus decode → 24kHz PCM]
    ↓ [Mimi encode → LM → Mimi decode]
    ↓ [PersonaPlex: PCM → Opus encode]
Opus bytes (from PersonaPlex)
```

### Outbound (PersonaPlex → Exotel)

```
Opus bytes (from PersonaPlex)
    ↓ [sphn.OpusStreamReader.append_bytes()]
PCM float32 48kHz
    ↓ [float32 → int16, clip to [-32767, 32767]]
Raw PCM16LE 48kHz mono
    ↓ [FFmpeg resample: 48k → 8k]
Raw PCM16LE 8kHz mono (1600 bytes)
    ↓ [base64 encode]
    ↓ [JSON: {"event":"media","media":{"payload":"..."}}]
Exotel WebSocket
```

## Frame Timing

| Stage | Duration | Buffer Size |
|-------|----------|-------------|
| Exotel chunk | 100ms | 1600 bytes (8kHz PCM) |
| Resampled (8k→48k) | 100ms | 9600 bytes (48kHz PCM) |
| Opus frame (20ms) | 20ms | ~40-120 bytes (variable) |
| PersonaPlex processing | Variable | Model-dependent |
| Resampled (48k→8k) | 100ms | 1600 bytes (8kHz PCM) |
| Exotel chunk | 100ms | 1600 bytes (8kHz PCM) |

## Error Handling Flow

```
Connection Error
    ↓
Handler exits (exotel_bridge.py:273)
    ↓
All tasks cancelled (exotel_bridge.py:274-275)
    ↓
PersonaPlex WS closed (exotel_bridge.py:277-280)
    ↓
FFmpeg processes killed (exotel_bridge.py:282-286)
    ↓
Handler cleanup complete
```

## Keepalive Mechanism

**Bridge → PersonaPlex:**
- Sends `0x00` every 2 seconds (exotel_bridge.py:132-140)
- Prevents WebSocket timeout

**PersonaPlex → Bridge:**
- Sends `0x00` after system prompts complete
- Bridge waits for this before starting audio flow

## Ports and Protocols Summary

| Component | Port | Protocol | Scheme |
|-----------|------|----------|--------|
| PersonaPlex Engine | 8998 | HTTPS/WSS | `wss://` |
| Exotel Bridge | 5050 | WS/WSS | `ws://` or `wss://` (via tunnel) |
| Tunnel (ngrok) | Dynamic | HTTPS/WSS | `wss://` (public) |
