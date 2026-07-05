# Production deployment

This guide documents a conservative Raspberry Pi deployment for the packaged
`callstack serve` HTTP server. It is documentation only: do not paste real API
keys, SIM PINs, SIM/customer numbers, SMS bodies, USSD responses, modem serials,
or SIM/modem identifiers into docs, issues, logs, or support transcripts.

The examples keep the HTTP API on loopback. If you bind to a non-loopback host,
keep API keys enabled and place the service behind an equivalent trusted network
boundary before exposing SMS, USSD, health, or metrics endpoints.

## Install with server and SQLite support

Install Callstack in a virtual environment and include the server plus SQLite
extras used by the HTTP API and durable SMS store:

```bash
python3 -m venv /opt/callstack/venv
/opt/callstack/venv/bin/pip install -e ".[server,sqlite]"
```

Use the safe doctor flow to identify candidate ports before enabling an
unattended service. `callstack doctor` uses non-mutating identity/attention
commands; it does not send SMS, USSD, SIM unlock, call, or storage-mutating
commands.

```bash
/opt/callstack/venv/bin/callstack doctor --ports /dev/ttyUSB2,/dev/ttyUSB3
```

## Local files and permissions

Keep configuration and secrets in local files with restrictive permissions. The
SQLite parent directory must exist and be writable by the service user before
`CALLSTACK_SMS_DB_PATH` points at it.

```bash
getent group callstack >/dev/null 2>&1 || groupadd --system callstack
id -u callstack >/dev/null 2>&1 || useradd --system --gid callstack --home /var/lib/callstack --shell /usr/sbin/nologin callstack
install -d -m 750 -o root -g callstack /etc/callstack
install -d -m 700 -o callstack -g callstack /var/lib/callstack
install -m 640 -o root -g callstack /dev/null /etc/callstack/api-keys
install -m 640 -o root -g callstack /dev/null /etc/callstack/callstack.env
```

`/etc/callstack` stays root-owned while the `callstack` group can traverse it;
the service user can read the API-key/env files through its group and write only
to `/var/lib/callstack`.

Add one locally generated API key to `/etc/callstack/api-keys`; do not print it
in shell history, docs, logs, issues, or PRs. If a SIM PIN is required, prefer an
environment indirection instead of writing the PIN value into the env file.

A redacted env-file shape:

```bash
CALLSTACK_AT_PORT=/dev/ttyUSB2
CALLSTACK_AUDIO_PORT=/dev/ttyUSB4
CALLSTACK_SMS_DB_PATH=/var/lib/callstack/sms.sqlite3
CALLSTACK_LOG_LEVEL=WARNING
# CALLSTACK_SIM_PIN_ENV=CALLSTACK_SIM_PIN
```

## systemd unit template

Adapt the paths and service user for your host. Keep secrets in
`/etc/callstack/api-keys` or root-readable environment files, not in the unit
body.

```ini
[Unit]
Description=Callstack modem HTTP server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=callstack
Group=callstack
EnvironmentFile=/etc/callstack/callstack.env
ExecStart=/opt/callstack/venv/bin/callstack serve --host 127.0.0.1 --port 8080 --api-key-file /etc/callstack/api-keys
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

The template intentionally binds to `127.0.0.1`. For a non-loopback bind, keep
API keys on and use a trusted network boundary such as a VPN, reverse proxy with
access controls, or host firewall policy.

## Readiness and metrics smoke checks

Build the bearer header without echoing the key. These checks only call public
readiness and aggregate metrics endpoints; they do not send SMS, USSD, SIM,
call, storage-mutating, or modem-identity commands.

```bash
CALLSTACK_BEARER_HEADER="$(awk 'NF {print "Authorization: Bearer " $0; exit}' /etc/callstack/api-keys)"
test -n "$CALLSTACK_BEARER_HEADER"
curl -fsS -H "$CALLSTACK_BEARER_HEADER" http://127.0.0.1:8080/healthz
curl -fsS -H "$CALLSTACK_BEARER_HEADER" http://127.0.0.1:8080/metrics | grep '^callstack_uptime_seconds '
```

Callstack's Prometheus output is designed to be PII-safe: it uses aggregate
counters and gauges with bounded labels and excludes phone numbers, SMS bodies,
USSD text, webhook URLs, SIM identifiers, API keys, modem serials, and raw AT
lines. Treat `/metrics` as part of the same HTTP surface as SMS and USSD routes:
keep it authenticated or reachable only across the same trusted boundary.

## Operational notes

- Keep `CALLSTACK_LOG_LEVEL=WARNING` for unattended deployments unless actively
  debugging with sanitized transcripts.
- Rotate API keys by editing `/etc/callstack/api-keys` locally and restarting
  the service; never paste the old or new value into issue trackers.
- Start with loopback-only serving and add network exposure only after the smoke
  checks pass with authentication enabled.
- Keep active modem scan/config-preview follow-ups separate from this deployment
  guide; safe discovery work is tracked under the modem-discovery roadmap.
