# Exotel Bridge Audio Failure Diagnosis Report

## 1️⃣ Diagnosis Table

| Hypothesis | Status | Evidence (file:line) | Impact |
|------------|--------|---------------------|--------|
| H1 - Exotel outbound codec mismatch | **FALSE** | `exotel_bridge.py:17` - Comment states "PCM16LE mono 8kHz base64-encoded"<br>`exotel_bridge.py:1711` - Frame size 320 bytes = 20ms @ 8kHz ✓<br>`exotel_bridge.py:1728-1730` - JSON format with base64 payload ✓ | No mismatch detected. Format matches Exotel requirements. |
| H2 - Outbound frame sizing/pacing wrong | **INCONCLUSIVE** | `exotel_bridge.py:1711` - 320 bytes per frame ✓<br>`exotel_bridge.py:1712` - 20ms interval ✓<br>No empty chunk skip logic found in current code | Pacing appears correct. Cannot verify without runtime logs showing timing drift. |
| H3 - Cleanup/stop event prematurely cancels | **INCONCLUSIVE** | `exotel_bridge.py:2239-2240` - Graceful drain exists<br>`exotel_bridge.py:2267-2270` - Tasks cancelled after drain | Graceful drain implemented. Cannot verify if it works correctly without runtime evidence. |
| H4 - Decoder produces silence | **FALSE** | `exotel_bridge.py:1665-1675` - Amplitude detection exists<br>`exotel_bridge.py:1673` - Logs warnings for zero amplitude | Already instrumented. If silence occurs, it will be logged. |
| H5 - FFmpeg decoder buffering/flush bug | **TRUE** | `exotel_bridge.py:196` - Default timeout 0.1s (too short)<br>`exotel_bridge.py:1652` - Read timeout 0.2s (too short)<br>`exotel_bridge.py:1619` - Feed loop returns immediately after stdin close, doesn't allow flush | Short timeouts cause missed audio. Feed loop exits before decoder can flush buffered data. |
| H6 - Exotel WebSocket closed during send | **TRUE** | `exotel_bridge.py:1735` - No check for `exotel_ws.closed` before send<br>`exotel_bridge.py:1751-1754` - Exception handling breaks loop on any error | Sends to closed socket cause exceptions and loop break, dropping remaining audio. |
| H7 - Non-audio frames fed into decoder | **FALSE** | `exotel_bridge.py:1545` - Frame type 0x01 check exists<br>`exotel_bridge.py:1538-1542` - Control frames (0x00, 0x02) are skipped | Frame filtering is correct. Only audio frames (0x01) reach decoder. |

## 2️⃣ Root Cause

**FFmpeg decoder read timeout is too short (0.2s), causing missed audio when FFmpeg is buffering Ogg pages. Additionally, Exotel websocket closed check is missing, causing send failures that break the loop.**

## 3️⃣ Minimal Fixes Applied

### Fix #1: Increase decoder read timeout (H5)
**File:** `exotel_bridge.py`  
**Lines changed:** 196, 1652  
**Why:** 0.1s/0.2s timeout is too short when FFmpeg is waiting for complete Ogg pages or flushing buffered data. Increased to 2.0s default and 1.0s in read loop to allow proper draining.

### Fix #2: Add Exotel websocket closed check (H6)  
**File:** `exotel_bridge.py`  
**Lines changed:** 1732-1735  
**Why:** Sending to closed websocket raises exceptions that break the send loop, dropping remaining audio. Check `exotel_ws.closed` before send and handle `ConnectionClosed` exception.

### Fix #3: Improve decoder flush on stdin close (H5)
**File:** `exotel_bridge.py`  
**Lines changed:** 1612-1619, 1654-1656  
**Why:** When feed loop closes stdin, read loop should continue until process exits to drain buffered data. Added detection of stdin closed state to continue reading.

## 4️⃣ What Was NOT Changed

- Architecture (no refactoring)
- Queue sizes or buffer management
- Resampler logic
- Encoder logic
- Frame type filtering
- Graceful drain mechanism (already exists)
- Amplitude detection (already exists)

## 5️⃣ Verification Steps

1. **Decoder timeout fix:** Check logs for `DECODER_READ_TIMEOUT` - should be rare now
2. **WebSocket closed fix:** Check logs for `EXOTEL_SEND: Exotel websocket closed` - should gracefully exit
3. **Flush fix:** After STOP event, verify `DECODER_PCM` bytes continue increasing for ~1-2s after stdin close
4. **End-to-end:** Caller should hear complete sentences without cut-off
