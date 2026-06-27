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
- ✅ HTTP observability: `/healthz` and PII-safe Prometheus `/metrics` expose aggregate readiness and runtime counters.
- ✅ CLI groundwork: `callstack status`, `callstack send`, and safe `callstack doctor` support local Pi operations and hardware bring-up.
- ✅ Conservative modem profile helpers classify known modem identities without mutating hardware state.

---

## Phase 2 — SMS + Realtime Enhancements (v0.3)

### Multi-Part SMS Reassembly
- Current state: UDH metadata parsing exists.
- Next step: reassemble long messages in `SMSService`, persist grouped parts, and emit one public incoming-message event when complete.

### WebSocket Real-Time Feed
- `/ws` endpoint for live event streaming: SMS, delivery reports, call state, signal quality, and USSD responses.
- Keep API-key authentication, bounded labels/payloads, and no raw SMS bodies, phone numbers, USSD text, SIM identifiers, or modem serials in broadcast metadata unless an explicit authenticated consumer requests them.

### Observability Follow-Ups
- `/healthz` and `/metrics` are shipped; next observability work should focus on auth defaults, deployment config, and documenting production-safe scrape patterns.
- Add PII-safe local event tailing through `callstack monitor`.

### Modem Auto-Detection
- Safe explicit-port `callstack doctor` probing is shipped.
- Next step: active auto-detection that can scan candidate `/dev/ttyUSB*` ports, choose AT/audio assignments conservatively, and keep all probes non-mutating.

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
- Shipped: `callstack send`, `callstack status`, and safe `callstack doctor`.
- Planned: PII-safe `callstack monitor`, richer config/env loading, and deployment-friendly examples.

---

## Next hardening order

Prefer these small, reviewable slices before broad realtime/dashboard expansion:

1. Auth and secret hygiene: redacted environment config (#58), privacy-safe default logging (#61), and deployment-safe auth defaults (#4).
2. SMS correctness: text-mode inbound body fidelity (#72), multipart reassembly/finality (#10), delivery-report edge cases, and continued recipient-validation regression coverage.
3. Webhook safety: URL admission and dispatch hardening (#47), signed delivery with retry/backoff (#21), and bounded error logs.
4. Operator DX: reconcile the shipped safe doctor command with its tracking issue (#57), then add PII-safe `callstack monitor` (#50), production-safe health/metrics deployment notes, and explicit modem discovery/autoconnect follow-ups (#11).
5. Realtime and PBX: WebSocket event streaming (#31), scheduled SMS (#49), pre-answer routing (#40), voicemail helpers (#41), and IVR/DTMF hardening once SMS/security foundations stay green.

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
| P0 | SMS/security hardening | Small-Medium | High | Continue recipient validation, text-mode fidelity, auth, redaction, and webhook safety |
| P1 | WebSocket Feed | Medium | High | Planned after SMS/security foundations |
| P1 | PII-safe CLI monitor | Low-Medium | Medium | Planned; status/send/doctor are shipped |
| P1 | Modem Auto-Detection | Medium | High | Safe explicit-port doctor shipped; active scanning/assignment planned |
| P2 | Voicemail System | Medium | High | Planned |
| P2 | GPS/GNSS | Medium | High | Planned |
| P2 | Scheduled SMS | Low | Medium | Planned |
| P3 | Web Dashboard | High | Medium | Planned after realtime/security foundations |
| P3 | Multi-Modem | High | High | Planned |
| P3 | Plugin System | Medium | Medium | Planned |
