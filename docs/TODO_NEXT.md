# PersonaPlex × Exotel Bridge - Next Steps & Known Issues

## Critical Issues (High Priority)

### 1. Sample Rate Mismatch Bug ⚠️ **CRITICAL**

**Location:** `exotel_bridge.py:29`

**Issue:**
- Bridge uses `MODEL_SR = 48000` but PersonaPlex model expects `24000 Hz`
- This causes audio speed/pitch issues

**Impact:** Audio quality degradation, incorrect playback speed

**Fix:**
```python
# Change line 29 in exotel_bridge.py
MODEL_SR = 24000  # Was: 48000
```

**Complexity:** Trivial (1 line change)

**Files to Update:**
- `exotel_bridge.py:29` - MODEL_SR constant
- `exotel_bridge.py:147-148` - FFmpeg resampler commands (8k→24k, 24k→8k)
- `exotel_bridge.py:161` - Frame size calculation (20ms @ 24kHz = 480 samples)

---

### 2. Missing Reconnection Logic

**Location:** `exotel_bridge.py:106-288`

**Issue:**
- Bridge does not handle PersonaPlex disconnections
- If PersonaPlex restarts, bridge connections fail permanently
- No retry mechanism for failed connections

**Impact:** Service downtime requires manual restart

**Fix:**
- Add retry loop in `handler()` function
- Implement exponential backoff
- Add connection health checks

**Complexity:** Medium (requires async retry logic)

**Estimated Time:** 2-4 hours

---

### 3. Hardcoded Configuration

**Location:** `exotel_bridge.py:12-25`

**Issue:**
- Bridge settings are hardcoded (host, port, voice prompt, text prompt)
- Cannot configure via environment variables or CLI args
- Requires code changes for different deployments

**Impact:** Inflexible deployment, requires code edits

**Fix:**
- Add environment variable support
- Add CLI argument parser (argparse)
- Support config file (YAML/JSON)

**Complexity:** Low-Medium (standard Python patterns)

**Estimated Time:** 1-2 hours

---

## Important Improvements (Medium Priority)

### 4. Error Recovery for FFmpeg Processes

**Location:** `exotel_bridge.py:41-71, 147-150`

**Issue:**
- FFmpeg subprocess failures are not retried
- Process death causes permanent audio pipeline failure
- No monitoring of subprocess health

**Impact:** Audio stops working, requires connection restart

**Fix:**
- Add subprocess health monitoring
- Restart failed processes automatically
- Add process exit handlers

**Complexity:** Medium (requires subprocess management)

**Estimated Time:** 2-3 hours

---

### 5. Missing Health Check Endpoint

**Location:** Bridge service (new)

**Issue:**
- No `/health` endpoint for monitoring
- Cannot verify bridge status without making a connection
- Difficult to integrate with monitoring systems

**Impact:** No visibility into service health

**Fix:**
- Add HTTP health endpoint (separate from WebSocket)
- Return status of PersonaPlex connection
- Include version/metadata

**Complexity:** Low (simple HTTP endpoint)

**Estimated Time:** 1 hour

---

### 6. No Authentication on Bridge

**Location:** `exotel_bridge.py:291-294`

**Issue:**
- Bridge WebSocket endpoint has no authentication
- Anyone can connect if they know the URL
- No rate limiting or access control

**Impact:** Security risk, potential abuse

**Fix:**
- Add API key/token authentication
- Validate Exotel-specific headers (if available)
- Add rate limiting

**Complexity:** Medium (requires auth middleware)

**Estimated Time:** 2-3 hours

---

### 7. Logging Improvements

**Location:** Throughout `exotel_bridge.py`

**Issue:**
- Logging is basic (print statements)
- No log levels (INFO, ERROR, DEBUG)
- No structured logging (JSON)
- Difficult to debug production issues

**Impact:** Poor observability

**Fix:**
- Use Python `logging` module
- Add log levels and formatters
- Optional structured logging (JSON)
- Add request IDs for tracing

**Complexity:** Low (standard library)

**Estimated Time:** 1-2 hours

---

## Nice-to-Have Features (Low Priority)

### 8. Metrics and Monitoring

**Location:** New module

**Issue:**
- No metrics collection (latency, throughput, errors)
- Cannot track call quality or performance
- No alerting capabilities

**Impact:** Limited observability

**Fix:**
- Add Prometheus metrics endpoint
- Track: connection count, audio frame rate, latency, errors
- Optional: Grafana dashboard

**Complexity:** Medium (requires metrics library)

**Estimated Time:** 3-4 hours

---

### 9. Support Multiple Concurrent Calls

**Location:** `exotel_bridge.py:291-294`

**Issue:**
- Current implementation supports one call at a time
- Multiple Exotel connections may interfere
- No connection isolation

**Impact:** Cannot handle multiple simultaneous calls

**Fix:**
- Verify current implementation (may already support multiple)
- Add connection pooling if needed
- Test with multiple concurrent connections

**Complexity:** Low-Medium (depends on current behavior)

**Estimated Time:** 2-3 hours

---

### 10. Dynamic Voice/Text Prompt Selection

**Location:** `exotel_bridge.py:16-17, 21-25`

**Issue:**
- Voice and text prompts are hardcoded
- Cannot change per-call or per-Exotel number
- Requires code changes for different personas

**Impact:** Inflexible persona configuration

**Fix:**
- Accept prompts via WebSocket handshake (query params or initial message)
- Support prompt selection based on Exotel number
- Add prompt validation

**Complexity:** Medium (requires protocol changes)

**Estimated Time:** 3-4 hours

---

### 11. Audio Quality Tuning

**Location:** `exotel_bridge.py:156-162`

**Issue:**
- Opus encoding parameters are default
- No control over bitrate, complexity, frame size
- May not be optimal for telephony

**Impact:** Suboptimal audio quality

**Fix:**
- Add Opus encoding parameter configuration
- Tune for telephony use case (low latency, good quality)
- Add A/B testing capability

**Complexity:** Low-Medium (requires Opus parameter knowledge)

**Estimated Time:** 2-3 hours

---

### 12. Docker Compose Integration

**Location:** `docker-compose.yaml`

**Issue:**
- Bridge is not included in docker-compose
- Must run bridge separately
- No unified deployment

**Impact:** More complex deployment

**Fix:**
- Add bridge service to docker-compose.yaml
- Configure networking between services
- Add health checks

**Complexity:** Low (standard Docker Compose)

**Estimated Time:** 1 hour

---

## Testing & Validation

### 13. Unit Tests

**Location:** New `tests/` directory

**Issue:**
- No unit tests for bridge logic
- No integration tests
- Manual testing only

**Impact:** Risk of regressions, difficult to verify fixes

**Fix:**
- Add pytest test suite
- Test: audio format conversion, WebSocket protocol, error handling
- Add CI/CD pipeline

**Complexity:** Medium (requires test infrastructure)

**Estimated Time:** 4-6 hours

---

### 14. End-to-End Test Suite

**Location:** New `tests/e2e/` directory

**Issue:**
- No automated E2E tests
- Manual testing with Exotel required
- Difficult to reproduce issues

**Impact:** Slow validation cycle

**Fix:**
- Mock Exotel WebSocket client
- Simulate call flow
- Validate audio round-trip

**Complexity:** Medium-High (requires mock infrastructure)

**Estimated Time:** 6-8 hours

---

## Documentation

### 15. API Documentation

**Location:** `docs/API.md` (new)

**Issue:**
- No API documentation for bridge WebSocket protocol
- Exotel integration details not documented
- Message format examples missing

**Impact:** Difficult for others to integrate

**Fix:**
- Document WebSocket message formats
- Add example payloads
- Document error codes

**Complexity:** Low (documentation)

**Estimated Time:** 2 hours

---

## Deployment & Operations

### 16. Production Deployment Guide

**Location:** `docs/DEPLOYMENT.md` (new)

**Issue:**
- No production deployment instructions
- No systemd service files
- No process management guidance

**Impact:** Difficult to deploy in production

**Fix:**
- Create systemd service files
- Add process manager configuration (supervisor/PM2)
- Document production best practices

**Complexity:** Low-Medium (standard deployment patterns)

**Estimated Time:** 2-3 hours

---

### 17. Monitoring & Alerting Setup

**Location:** New configuration files

**Issue:**
- No monitoring setup documented
- No alerting rules
- No dashboards

**Impact:** Limited production visibility

**Fix:**
- Add Prometheus exporter
- Create Grafana dashboards
- Define alerting rules (PagerDuty/OpsGenie)

**Complexity:** Medium (requires monitoring stack)

**Estimated Time:** 4-6 hours

---

## Priority Summary

### Immediate (Do First)
1. **Sample Rate Mismatch Bug** - Fix audio quality issue
2. **Missing Reconnection Logic** - Improve reliability
3. **Hardcoded Configuration** - Enable flexible deployment

### Short Term (Next Sprint)
4. Error Recovery for FFmpeg
5. Health Check Endpoint
6. Logging Improvements

### Medium Term (Future)
7. Authentication
8. Metrics & Monitoring
9. Dynamic Prompt Selection

### Long Term (Backlog)
10. Unit Tests
11. E2E Tests
12. Production Deployment Guide

## Estimated Total Effort

- **Critical Issues:** 4-7 hours
- **Important Improvements:** 6-9 hours
- **Nice-to-Have:** 15-20 hours
- **Testing & Documentation:** 12-16 hours

**Total:** ~37-52 hours of development work

## Current State Assessment

### What Works
- ✅ PersonaPlex engine runs and serves WebSocket endpoint
- ✅ Bridge connects to PersonaPlex and forwards audio
- ✅ Basic audio flow (Exotel → Bridge → PersonaPlex → Bridge → Exotel)
- ✅ FFmpeg resampling pipeline
- ✅ Opus encoding/decoding

### What's Broken
- ⚠️ Sample rate mismatch (48000 vs 24000) - causes audio issues
- ⚠️ No reconnection on PersonaPlex restart
- ⚠️ Hardcoded configuration (not production-ready)

### What's Missing
- ❌ Health checks
- ❌ Authentication
- ❌ Error recovery
- ❌ Monitoring/metrics
- ❌ Production deployment setup
- ❌ Tests

## Next Immediate Actions

1. **Fix sample rate bug** (5 minutes)
   - Change `MODEL_SR = 48000` to `MODEL_SR = 24000`
   - Update resampler commands
   - Test audio quality

2. **Add environment variable support** (1 hour)
   - Add `BRIDGE_HOST`, `BRIDGE_PORT`, `VOICE_PROMPT`, `TEXT_PROMPT` env vars
   - Update bridge code to read from env

3. **Add reconnection logic** (2-3 hours)
   - Implement retry loop in handler
   - Add exponential backoff
   - Test with PersonaPlex restart

4. **Test end-to-end** (1 hour)
   - Start PersonaPlex
   - Start bridge
   - Make test call via Exotel
   - Verify audio quality
