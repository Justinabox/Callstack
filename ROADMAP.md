# Callstack Roadmap

## Phase 1 — Core Gaps (v0.2)

### SIM PIN Management
- `AT+CPIN` handling: detect locked SIM on connect, unlock with PIN, PUK recovery
- Critical for production deployments where SIMs reboot locked

### API Authentication
- API key middleware for the HTTP server
- Rate limiting per key
- Currently zero auth — security blocker

### SMS Delivery Reports
- Wire up existing `+CDSI` URC fully
- Track delivery status per message: pending -> delivered -> failed
- Expose via events and HTTP API

### DTMF Send
- `AT+VTS` to send DTMF tones during active calls
- Needed for navigating automated phone trees programmatically

### USSD Support
- `AT+CUSD` for balance checks, carrier menus, short codes
- New `USSDService` with send/receive and async events

---

## Phase 2 — SMS Enhancements (v0.3)

### Multi-Part (Concatenated) SMS
- UDH parsing to reassemble long messages split by the network

### WebSocket Real-Time Feed
- `/ws` endpoint for live event streaming (SMS, call state, signal quality)

### Prometheus/Metrics Endpoint
- `/metrics` with messages sent/received/failed, active calls, signal quality, uptime

### Modem Auto-Detection
- Scan `/dev/ttyUSB*`, probe with `ATI`, auto-detect model and port assignment

---

## Phase 3 — Voice & Audio (v0.4)

### Voicemail System
- No-answer detection -> greeting -> record -> store as WAV with metadata
- HTTP API for retrieval

### Call Recording (Full Duplex)
- Tap AudioPipeline to record both sides to WAV during active calls

### GPS/GNSS Integration
- `AT+CGNSPWR` / `AT+CGNSINF` for SIM868 built-in GPS
- New `LocationService` with position events

### Scheduled SMS
- `POST /sms/schedule` with `send_at` timestamp, server-side queue

---

## Phase 4 — Developer Experience (v0.5)

### CLI Tool
- `callstack send`, `callstack status`, `callstack monitor`

### Plugin/Middleware System
- Hook into event pipeline: auto-reply, spam filtering, message transforms

### Web Dashboard
- Lightweight HTML dashboard: signal strength, message log, call history, send form

---

## Phase 5 — Scale & Hardware (v1.0)

### Multi-Modem Support
- `ModemPool` orchestrator: load balancing, failover, round-robin

### Broader Modem Support
- Test matrix for Quectel EC25/EG25, Huawei MU709, Sierra Wireless
- Modem-specific driver layer with community profiles

### Call Transfer & Conference
- Blind/attended transfer via `AT+CHLD`
- Conference calling via `AT+CHLD=3`

### Mobile Data / PDP Context
- `AT+CGDCONT` / `AT+CGACT` for cellular data connections

### MMS Support
- Basic MMS send/receive via AT commands or modem HTTP stack

---

## Priority Matrix

| Priority | Feature              | Effort | Impact |
|----------|----------------------|--------|--------|
| P0       | SIM PIN Management   | Low    | High   |
| P0       | API Authentication   | Low    | High   |
| P0       | SMS Delivery Reports | Low    | High   |
| P1       | USSD Support         | Low    | Medium |
| P1       | DTMF Send           | Low    | Medium |
| P1       | Multi-Part SMS       | Medium | High   |
| P1       | WebSocket Feed       | Medium | High   |
| P1       | Metrics Endpoint     | Low    | Medium |
| P2       | Voicemail System     | Medium | High   |
| P2       | Call Recording       | Medium | Medium |
| P2       | GPS/GNSS             | Medium | High   |
| P2       | Scheduled SMS        | Low    | Medium |
| P3       | Web Dashboard        | High   | Medium |
| P3       | Multi-Modem          | High   | High   |
| P3       | Plugin System        | Medium | Medium |
