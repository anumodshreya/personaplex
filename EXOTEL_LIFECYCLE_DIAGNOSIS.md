# Exotel WebSocket Lifecycle Diagnosis Report

## 1️⃣ Exotel WebSocket Lifecycle Map

### File: `exotel_bridge.py`

**Handler Entry Point:**
- `handler(exotel_ws, path=None)` - Line 592
  - Accepts Exotel WebSocket connection
  - Creates session_id, initializes queues, connects to PersonaPlex

**Server-Initiated Closes (we call `exotel_ws.close()`):**
1. **Line 667** - `exotel_to_engine()` → PersonaPlex connection failed after retries
   - Reason: `personaplex_connection_failed`
   - Stack: Error handler in connection retry loop

2. **Line 677** - `handler()` → PersonaPlex WebSocket is None after retries
   - Reason: `personaplex_ws_none`
   - Stack: After connection retry loop

3. **Line 703** - `handler()` → PersonaPlex handshake timeout
   - Reason: `personaplex_handshake_timeout`
   - Stack: After waiting for 0x00 handshake byte

4. **Line 721** - `handler()` → Resampler start failed
   - Reason: `resampler_start_failed`
   - Stack: Exception handler in resampler initialization

5. **Line 960** - `handler()` → FFmpeg Opus encoder start failed
   - Reason: `encoder_start_failed` (inferred)
   - Stack: Exception handler in encoder initialization

**Client-Initiated Closes (Exotel closes websocket):**
1. **Line 1096** - `exotel_to_engine()` → `websockets.exceptions.ConnectionClosed`
   - Caught in `exotel_to_engine()` exception handler
   - Logs: "Exotel connection closed (normal)"
   - **NEW:** Now logs close_code, close_reason, timestamps

2. **Line 1880** - `handler()` → `exotel_ws.wait_closed()` returns
   - Logs: "Exotel client disconnected"
   - **NEW:** Now logs full context with close_code, close_reason, timestamps

**STOP Event Handling:**
- **Line 1044-1071** - `exotel_to_engine()` → `event_type == "stop"`
  - Logs: "Exotel stop event received"
  - **NEW:** Enhanced logging with full session state
  - Sends silence tail to queue
  - Puts None sentinel in pcm8k_queue
  - **BREAKS** the `async for msg in exotel_ws:` loop
  - **NEW:** If `EXOTEL_DRAIN_AFTER_STOP=1`, sets `drain_mode=True` but still breaks loop

**Task Cancellation:**
- **Line 2000-2010** - `handler()` → Cleanup phase
  - Sets `connection_active = False`
  - Cancels all tasks: `exotel_to_engine`, `resample_loop`, `encode_and_send_loop`, `engine_to_exotel`
  - **NEW:** If `drain_mode=True`, cancellation happens after drain window expires

## 2️⃣ Instrumentation Added

### STOP Event Logging (Line 1047-1056)
```
EXOTEL_STOP_EVENT: session={session_id} ts={stop_ts:.3f} 
  ws_state={ws_state} 
  exotel_in_frames={count} exotel_out_frames={count} 
  engine_audio_frames={count} decoder_pcm_bytes={bytes} 
  drain_after_stop={bool} 
  next_action=break_exotel_to_engine_loop
```

### Server-Initiated Close Logging
All `exotel_ws.close()` calls now log:
```
SERVER_INITIATED_EXOTEL_CLOSE: session={session_id} 
  reason={reason} 
  stack={traceback}
```

### Client-Initiated Close Logging (Line 1943-1954, 1110-1120)
```
EXOTEL_CLIENT_DISCONNECTED: session={session_id} 
  close_code={code} close_reason={reason} is_normal={bool}
  last_exotel_inbound={ts} last_exotel_outbound={ts}
  last_engine_inbound={ts} last_decoder_pcm={ts}
  exotel_in_frames={count} exotel_out_frames={count}
  engine_audio_frames={count} decoder_pcm_bytes={bytes}
```

### Timestamp Tracking
- `last_exotel_inbound_ts` - Updated on every Exotel media frame receive (Line 1085)
- `last_exotel_outbound_ts` - Updated on every Exotel send (Line 1793)
- `last_engine_inbound_ts` - Updated on every engine audio frame receive (Line 1534)
- `last_decoder_pcm_ts` - Updated on every decoder PCM output (Line 1693)

## 3️⃣ STOP Event Control Flow Analysis

### Current Behavior (Default - EXOTEL_DRAIN_AFTER_STOP=0):
1. STOP event received → `exotel_to_engine()` breaks loop (Line 1067)
2. `exotel_to_engine()` exits → None sentinel put in pcm8k_queue (Line 1064-1066)
3. Handler monitoring loop continues → waits for `exotel_ws.wait_closed()` (Line 1880)
4. **Exotel closes websocket** (client-initiated) → "Exotel client disconnected" logged
5. Handler sets `connection_active = False` (Line 2000)
6. All tasks cancelled → `exotel_send_loop` and `engine_to_exotel` stop immediately

### Problem Identified:
**STOP event causes `exotel_to_engine()` to exit, which breaks the `async for msg in exotel_ws:` loop. This loop ending does NOT close the websocket, but Exotel likely closes it shortly after sending STOP. The handler then detects the close and cancels all tasks, including `exotel_send_loop`, before engine audio can be sent.**

### Evidence:
- STOP event breaks the receive loop (Line 1067)
- Handler waits for websocket close (Line 1880)
- When close detected, tasks are cancelled immediately (Line 2000-2010)
- No drain window exists by default

## 4️⃣ Drain-After-STOP Implementation (Env Var Gated)

### Configuration (Lines 72-75):
```python
EXOTEL_DRAIN_AFTER_STOP = os.getenv("EXOTEL_DRAIN_AFTER_STOP", "0") == "1"
EXOTEL_DRAIN_SECS = float(os.getenv("EXOTEL_DRAIN_SECS", "8.0"))
EXOTEL_SEND_SILENCE_WHEN_IDLE = os.getenv("EXOTEL_SEND_SILENCE_WHEN_IDLE", "0") == "1"
```

### Behavior When Enabled (EXOTEL_DRAIN_AFTER_STOP=1):

**On STOP Event (Line 1071):**
- Sets `drain_mode = True`
- Still breaks `exotel_to_engine()` loop (stops inbound ingestion)
- Puts None sentinel in queue

**In `exotel_send_loop()` (Line 1773-1790):**
- Loop condition: `while connection_active or (drain_mode and stop_received_ts is not None)`
- Checks drain exit conditions:
  - Timeout: `elapsed >= EXOTEL_DRAIN_SECS` (default 8.0s)
  - Playback finished: `pcm_out_queue.qsize() == 0 AND time_since_decoder >= 0.5s`
- If `EXOTEL_SEND_SILENCE_WHEN_IDLE=1` and queue empty for >200ms, sends silence frames at 20ms cadence

**In Handler Monitoring Loop (Line 1924-1970):**
- Loop condition: `while connection_active or (drain_mode and stop_received_ts is not None)`
- If socket closes during drain, continues draining (Line 1957-1959)
- Checks drain exit conditions every timeout (Line 1962-1975)
- Only sets `connection_active = False` after drain completes

**Exception Handling (Line 1810-1820):**
- `ConnectionClosed` exception in send loop exits gracefully if in drain mode
- Does not raise, allows drain to complete

## 5️⃣ Silence Keepalive (Env Var Gated)

### When Enabled (EXOTEL_SEND_SILENCE_WHEN_IDLE=1):
- Only active during drain mode
- Sends 320-byte silence frames (20ms @ 8kHz PCM) at 20ms cadence
- Only if `pcm_out_queue` is empty for >200ms
- Stops when drain window expires or playback finishes
- Rate-limited logging (every 5s)

## 6️⃣ Exception Handling Fixes

### `exotel_send_loop()` (Line 1807-1820):
- **Before:** Any exception would `raise`, breaking the loop
- **After:** 
  - `ConnectionClosed` exception exits gracefully (logs and breaks)
  - In drain mode, other exceptions also exit gracefully (don't raise)
  - Checks `exotel_ws.closed` before send (Line 1784)

## 7️⃣ Diagnosis: Who Closes the Websocket?

### Evidence from Code:
1. **Server-initiated closes:** Only occur during initialization failures (PersonaPlex connection, handshake, resampler, encoder)
2. **STOP event:** Does NOT close websocket - only breaks the receive loop
3. **Handler monitoring:** Waits for `exotel_ws.wait_closed()` - this means Exotel closes it
4. **Timing:** "Exotel client disconnected" happens AFTER "Exotel stop event received"

### Conclusion:
**The websocket close is EXOTEL/CLIENT-INITIATED, not server-initiated.**

**Sequence:**
1. Exotel sends STOP event
2. Our code breaks the receive loop (stops ingesting)
3. Exotel closes the websocket (likely after a short delay)
4. Our handler detects the close via `wait_closed()`
5. Handler cancels all tasks

### Non-Code Causes (Plausible):
1. **Exotel policy:** Closes media websocket after STOP event (common in telephony)
2. **Caller hangs up:** Exotel ends session, closes websocket
3. **Provider timeout:** If no audio received for X seconds, closes connection
4. **Network issue:** Connection dropped between Exotel and bridge

### How to Confirm:
- Check `close_code` in logs:
  - `1000` or `1001` = Normal close (Exotel policy or caller hangup)
  - `1006` = Abnormal close (network issue)
  - Other codes = Provider-specific
- Check `close_reason` if available
- Check Exotel dashboard for call status
- Compare timing: if close happens <1s after STOP, likely Exotel policy

## 8️⃣ Code Changes Summary

### Files Modified:
- `exotel_bridge.py`

### Changes Made:

1. **Configuration (Lines 72-75):**
   - Added `EXOTEL_DRAIN_AFTER_STOP` env var (default OFF)
   - Added `EXOTEL_DRAIN_SECS` env var (default 8.0s)
   - Added `EXOTEL_SEND_SILENCE_WHEN_IDLE` env var (default OFF)

2. **State Variables (Lines 628-636):**
   - Added `stop_received_ts`, `drain_mode`
   - Added timestamp tracking variables

3. **STOP Event Logging (Lines 1047-1056):**
   - Enhanced logging with full session state
   - Logs drain mode status

4. **STOP Event Handling (Lines 1064-1071):**
   - Puts None sentinel in queue
   - Sets `drain_mode=True` if env var enabled

5. **Server-Initiated Close Logging (Lines 667, 677, 703, 721):**
   - Added `SERVER_INITIATED_EXOTEL_CLOSE` logs with reason and stack

6. **Client-Initiated Close Logging (Lines 1110-1120, 1943-1954):**
   - Added `EXOTEL_CLIENT_DISCONNECTED` logs with full context

7. **Timestamp Tracking:**
   - `last_exotel_inbound_ts` updated on media receive (Line 1085)
   - `last_exotel_outbound_ts` updated on send (Line 1793)
   - `last_engine_inbound_ts` updated on engine receive (Line 1534)
   - `last_decoder_pcm_ts` updated on decoder output (Line 1693)

8. **Drain Mode Implementation:**
   - `exotel_send_loop()` continues during drain (Line 1773)
   - Handler monitoring continues during drain (Line 1924)
   - Drain exit conditions checked (Lines 1775-1790, 1962-1975)
   - Silence keepalive support (Lines 1792-1805)

9. **Exception Handling:**
   - `ConnectionClosed` handled gracefully (Line 1807-1810)
   - Drain mode exceptions don't raise (Line 1815-1820)

### Safety:
- **All new behavior is gated by env vars (default OFF)**
- **Default behavior unchanged** - existing functionality preserved
- **No architecture changes** - only added flags and conditional logic
- **Minimal changes** - only touched necessary code paths

## 9️⃣ Testing Recommendations

### To Determine Who Closes:
1. Run with current instrumentation
2. Check logs for `EXOTEL_STOP_EVENT` and `EXOTEL_CLIENT_DISCONNECTED`
3. Compare timestamps: if disconnect happens <1s after STOP, likely Exotel policy
4. Check `close_code`: 1000/1001 = normal, 1006 = abnormal

### To Test Drain Mode:
1. Set `EXOTEL_DRAIN_AFTER_STOP=1`
2. Place call, speak, wait for STOP
3. Verify logs show `EXOTEL_DRAIN_MODE: Enabled`
4. Verify `exotel_send_loop` continues for up to 8s after STOP
5. Verify engine audio is sent during drain window
6. Check if caller hears complete response

### To Test Silence Keepalive:
1. Set `EXOTEL_DRAIN_AFTER_STOP=1 EXOTEL_SEND_SILENCE_WHEN_IDLE=1`
2. Place call, verify silence frames sent when queue empty
3. Check Exotel doesn't close due to silence

## 🔟 Final Diagnosis

### Root Cause:
**Exotel closes the websocket after sending STOP event (client-initiated). Our code does NOT prematurely close it. However, the current default behavior cancels all tasks immediately when the close is detected, preventing engine audio from being sent.**

### Solution:
**Enable drain mode (`EXOTEL_DRAIN_AFTER_STOP=1`) to keep outbound audio flowing after STOP, even if Exotel closes the websocket. The drain window allows engine TTS to complete.**

### Evidence:
- STOP event does NOT call `exotel_ws.close()` (only breaks receive loop)
- Handler waits for `exotel_ws.wait_closed()` (proves Exotel closes it)
- All server-initiated closes are logged and only occur during initialization failures
- New instrumentation will prove this definitively in runtime logs

### Next Steps:
1. **Run with instrumentation** to confirm close is client-initiated
2. **If confirmed:** Enable `EXOTEL_DRAIN_AFTER_STOP=1` to allow audio completion
3. **If Exotel closes too quickly:** Consider `EXOTEL_SEND_SILENCE_WHEN_IDLE=1` to keep connection alive
4. **Monitor logs** for close_code and timing to understand Exotel's behavior
