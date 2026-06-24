# Callstack Roadmap

## Current progress

### Completed foundation (v0.2)
- ✅ SIM PIN management via `AT+CPIN`, including locked-SIM detection and PIN unlock flow.
- ✅ API key middleware for the HTTP server, with per-key rate limiting.
- ✅ SMS delivery report plumbing from `+CDSI` URCs through events and HTTP listing.
- ✅ DTMF send support with `AT+VTS` during active calls.
- ✅ USSD support via `AT+CUSD`, `USSDService`, events, and `/ussd/send`.
- ✅ Real-modem SMS prompt handling: serial reads now stop on the `> ` SMS prompt even without a newline.
- ✅ Packaging discovery scoped to `callstack*` so the flat-layout `audio/` directory does not break builds.
- ✅ Signal-quality polish: BER values now have human-readable descriptions.
- ✅ Multipart SMS groundwork: concatenated-message UDH metadata parser for 8-bit and 16-bit references.

---

## Phase 2 — SMS + Realtime Enhancements (v0.3)

### Multi-Part SMS Reassembly
- Current state: UDH metadata parsing exists.
- Next step: reassemble long messages in `SMSService`, persist grouped parts, and emit one public incoming-message event when complete.

### WebSocket Real-Time Feed
- `/ws` endpoint for live event streaming: SMS, delivery reports, call state, signal quality, and USSD responses.

### Prometheus/Metrics Endpoint
- `/metrics` with messages sent/received/failed, delivery-report counts, active calls, signal quality, and uptime.

### Modem Auto-Detection
- Scan `/dev/ttyUSB*`, probe with `ATI`, and auto-detect model plus AT/audio port assignment.

---

## Phase 3 — Voice & Audio (v0.4)

### Voicemail System
- No-answer detection → greeting → record → store WAV with metadata.
- HTTP API for retrieval.

### Call Recording (Full Duplex)
- Tap `AudioPipeline` to record both sides to WAV during active calls.

### GPS/GNSS Integration
- `AT+CGNSPWR` / `AT+CGNSINF` for SIM868 built-in GPS.
- New `LocationService` with position events.

### Scheduled SMS
- `POST /sms/schedule` with `send_at` timestamp and a server-side queue.

---

## Phase 4 — Developer Experience (v0.5)

### CLI Tool
- `callstack send`, `callstack status`, `callstack monitor`.

### Plugin/Middleware System
- Hook into event pipeline: auto-reply, spam filtering, message transforms.

### Web Dashboard
- Lightweight HTML dashboard: signal strength, message log, call history, send form.

---

## Phase 5 — Scale & Hardware (v1.0)

### Multi-Modem Support
- `ModemPool` orchestrator: load balancing, failover, round-robin.

### Broader Modem Support
- Test matrix for Quectel EC25/EG25, Huawei MU709, Sierra Wireless.
- Modem-specific driver layer with community profiles.

### Call Transfer & Conference
- Blind/attended transfer via `AT+CHLD`.
- Conference calling via `AT+CHLD=3`.

### Mobile Data / PDP Context
- `AT+CGDCONT` / `AT+CGACT` for cellular data connections.

### MMS Support
- Basic MMS send/receive via AT commands or modem HTTP stack.

---

## Priority Matrix

| Priority | Feature | Effort | Impact | Status |
|----------|---------|--------|--------|--------|
| P0 | Multi-Part SMS Reassembly | Medium | High | UDH parser done; service integration next |
| P1 | WebSocket Feed | Medium | High | Planned |
| P1 | Metrics Endpoint | Low | Medium | Planned |
| P1 | Modem Auto-Detection | Medium | High | Planned |
| P2 | Voicemail System | Medium | High | Planned |
| P2 | GPS/GNSS | Medium | High | Planned |
| P2 | Scheduled SMS | Low | Medium | Planned |
| P3 | Web Dashboard | High | Medium | Planned |
| P3 | Multi-Modem | High | High | Planned |
| P3 | Plugin System | Medium | Medium | Planned |
