# Production Deployment Guide (Raspberry Pi + systemd)

This guide covers running the packaged `callstack serve` HTTP server as a hardened,
long-lived systemd service on a Raspberry Pi (or any systemd Linux host).

It documents deployment mechanics only. Every value below is a local path, a loopback
address, or a placeholder — no API key, SIM identifier, phone number, or carrier code
appears here, and none should ever be pasted into this file, an issue, a PR, or a log.

## 1. Install the server runtime

Install the package with the server and durable SMS-store extras:

```bash
pip install -e ".[server,sqlite]"
```

This provides the `callstack` console script. Resolve the absolute path systemd needs for
`ExecStart`:

```bash
command -v callstack
```

Replace the `ExecStart=` binary path with that output when you write the unit in section 4.
`/usr/local/bin/callstack` is only an example; virtualenv, pipx, and distro-packaged
installs resolve elsewhere, and a wrong path fails the service at start with status 203.

## 2. Create the service account and local paths

Run these as root. The API-key file and the state directory are the only sensitive
locations, and both stay off-host-shared media:

```bash
useradd --system --no-create-home --shell /usr/sbin/nologin callstack
install -d -m 700 /etc/callstack
install -d -m 700 /var/lib/callstack
install -m 600 /dev/null /etc/callstack/api-keys
install -m 600 /dev/null /etc/callstack/callstack.env
chown -R callstack:callstack /etc/callstack /var/lib/callstack
```

| Path | Mode | Purpose |
| --- | --- | --- |
| `/etc/callstack` | `0700` | Configuration and secrets directory |
| `/etc/callstack/api-keys` | `0600` | One API key per line; consumed by `--api-key-file` |
| `/etc/callstack/callstack.env` | `0600` | `EnvironmentFile` for the unit |
| `/var/lib/callstack` | `0700` | Durable SMS store (SQLite) and other mutable state |

Write one locally generated API key per line into `/etc/callstack/api-keys` with an
editor or a redirect from your key generator. Do not print the file to a terminal that
is being recorded, and do not commit it.

The service account needs read access to the modem's serial devices. On Debian/Raspberry
Pi OS that is normally the `dialout` group:

```bash
usermod -aG dialout callstack
```

## 3. Write the environment file

`/etc/callstack/callstack.env` holds the redacted config the CLI and server already
share. Use `KEY=value` lines with no `export` and no quoting:

```ini
CALLSTACK_AT_PORT=/dev/ttyUSB2
CALLSTACK_AUDIO_PORT=/dev/ttyUSB4
CALLSTACK_SMS_DB_PATH=/var/lib/callstack/sms.sqlite3
CALLSTACK_LOG_LEVEL=WARNING
```

Keep `CALLSTACK_LOG_LEVEL` at `WARNING` or higher in production; verbose levels increase
the volume of modem/SMS diagnostics reaching the journal.

If your SIM needs a PIN, set `CALLSTACK_SIM_PIN_ENV` to the *name* of the variable that
carries it and define that variable in this same `0600` file — never inline the PIN into
the unit file, a shell command, or this guide.

## 4. Install the systemd unit

Write `/etc/systemd/system/callstack.service`:

```ini
[Unit]
Description=Callstack HTTP server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=callstack
Group=callstack
SupplementaryGroups=dialout
EnvironmentFile=/etc/callstack/callstack.env
ExecStart=/usr/local/bin/callstack serve --host 127.0.0.1 --port 8080 --api-key-file /etc/callstack/api-keys
Restart=on-failure
RestartSec=5

NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/callstack

[Install]
WantedBy=multi-user.target
```

Why these hardening directives:

- `NoNewPrivileges=yes` — the service can never gain privileges via setuid binaries.
- `PrivateTmp=yes` — private `/tmp`, so scratch files are not readable by other users.
- `ProtectSystem=strict` — the entire filesystem is read-only to the service.
- `ProtectHome=yes` — `/home`, `/root`, and `/run/user` are inaccessible.
- `ReadWritePaths=/var/lib/callstack` — the single writable exception, needed for the
  SQLite SMS store. `/etc/callstack` stays read-only; the server only reads it.

If you point `CALLSTACK_SMS_DB_PATH` somewhere other than `/var/lib/callstack`, add that
directory to `ReadWritePaths` or the server will fail to open its store.

Then enable it:

```bash
systemctl daemon-reload
systemctl enable --now callstack.service
systemctl status callstack.service
```

## 5. Health and metrics smoke checks

Both `/healthz` and `/metrics` are public-safe payloads (aggregate counters and readiness
only), but they are still behind bearer auth when API keys are configured. Read the first
local key straight into a header variable so the key never reaches your terminal, your
shell history, or the journal:

```bash
CALLSTACK_BEARER_HEADER="$(awk 'NF {print "Authorization: Bearer " $0; exit}' /etc/callstack/api-keys)"
test -n "$CALLSTACK_BEARER_HEADER"
curl -fsS -H "$CALLSTACK_BEARER_HEADER" http://127.0.0.1:8080/healthz
curl -fsS -H "$CALLSTACK_BEARER_HEADER" http://127.0.0.1:8080/metrics
unset CALLSTACK_BEARER_HEADER
```

Rules for this snippet:

- Never print the variable or the key file. There is no `echo`/`cat` step here on
  purpose — `test -n` confirms the header was built without revealing it.
- `-f` makes `curl` exit non-zero on `4xx`/`5xx`, so a failed auth or an unhealthy
  server fails the check instead of quietly printing an error body.
- `-sS` keeps the progress meter out of logs while still showing real transport errors.
- Run it as a user that can read `/etc/callstack/api-keys` (root or `callstack`).

For a Prometheus scrape, give the scraper its own key line in
`/etc/callstack/api-keys` and configure it via the scraper's own secret-file mechanism
(for example `authorization.credentials_file`). Do not inline a key into scrape config,
and keep the scrape target on the loopback interface or a trusted network boundary.

## 6. Binding beyond loopback

The unit above binds loopback only, which is the recommended posture: terminate TLS and
authenticate at a reverse proxy on the same host. If you deliberately need the server on
a network interface, change `ExecStart` to bind it:

```bash
callstack serve --host 0.0.0.0 --port 8080 --api-key-file /etc/callstack/api-keys
```

Non-loopback binds require API keys or an equivalent trusted network boundary. SMS, USSD,
and the realtime feed all reach a real SIM; an unauthenticated bind on a shared network is
an open gateway to sending messages and spending credit.

The loopback-only unauthenticated override (`--allow-unauthenticated-loopback`, or
`CALLSTACK_HTTP_ALLOW_UNAUTHENTICATED_LOOPBACK=1`) exists for development only and is
rejected for non-loopback hosts.

## 7. Webhooks and log hygiene

Incoming-SMS webhook subscriptions should target an endpoint on the same host or inside
your trusted network boundary. A public webhook target moves message content off the
device, so treat that as a deliberate data-egress decision rather than a default. This
guide intentionally contains no example webhook target — substitute your own internal
address.

Service logs go to the journal:

```bash
journalctl -u callstack.service -n 200
```

Before attaching journal output to an issue or PR, confirm it carries no phone numbers,
SMS or USSD payloads, SIM identifiers, or API keys.

## 8. Upgrades

```bash
systemctl stop callstack.service
pip install -e ".[server,sqlite]"
systemctl start callstack.service
```

Re-run the smoke checks in section 5 afterwards. `/var/lib/callstack` persists across
upgrades, so the SMS store survives restarts.
