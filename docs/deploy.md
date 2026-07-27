# Deploying loxo-llm-router

Serves on `:9090`, logs to stdout/stderr. Config: `loxo.toml` (see
`../loxo.toml.example`) + env for secrets (`../.env.example`).

## Docker (recommended, all platforms)

```bash
cp .env.example loxo.env          # fill in OPENROUTER_API_KEY
cp loxo.toml.example loxo.toml    # optional: edit your tier catalog
docker compose up --build -d
curl http://localhost:9090/health
```

Operational state (the spend ledger, and future ledgers) lives under
`LOXO_STATE_DIR` (default `~/.local/state/loxo-llm-router/`). The compose file
mounts a named volume there so ledgers survive rebuilds. A pre-existing ledger
at the legacy `~/.config/loxo-llm-router/spend.jsonl` keeps working — the
router logs a pointer; move the file to the state dir when convenient. The
`loxo-state` volume mounts the *default* state path only: if you override
`LOXO_STATE_DIR` or `XDG_STATE_HOME` in `loxo.env`, ledgers will be written
outside the volume and lost when the container is recreated — keep the
default inside containers, or adjust the mount to match your override.

If your model server runs on the host, set
`local_base_url = "http://host.docker.internal:7979/v1"` in `loxo.toml`.

## systemd (Linux, user service)

`~/.config/systemd/user/loxo-llm-router.service`:

```ini
[Unit]
Description=loxo-llm-router
After=network-online.target

[Service]
EnvironmentFile=%h/.config/loxo-llm-router/env
ExecStart=%h/.local/bin/loxo-llm-router
Environment=TZ=UTC
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now loxo-llm-router
journalctl --user -u loxo-llm-router -f
```

## launchd (macOS)

`~/Library/LaunchAgents/com.loxo.llm-router.plist` (find the binary with
`which loxo-llm-router`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.loxo.llm-router</string>
  <key>ProgramArguments</key>
  <array><string>/usr/local/bin/loxo-llm-router</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>TZ</key><string>UTC</string>
    <!-- Add OPENROUTER_API_KEY here: <key>OPENROUTER_API_KEY</key><string>sk-or-...</string> -->
  </dict>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/loxo-llm-router.out.log</string>
  <key>StandardErrorPath</key><string>/tmp/loxo-llm-router.err.log</string>
</dict>
</plist>
```

To inject the `OPENROUTER_API_KEY` secret, add it directly in the `EnvironmentVariables` dict (commented example shown above), or export it in your shell before running `launchctl bootstrap`.

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.loxo.llm-router.plist
```

## OpenTelemetry tracing (optional)

Loxo can emit one OTLP trace per request — route, reason, tokens, cost, latency,
and the local→cloud fallback hop as a child span. It is **off by default** and
strictly observe-only: enabling it never changes routing or the response.

Install the extra:

```bash
pip install 'loxo-llm-router[otel]'
```

Enable by pointing at any OTLP/HTTP endpoint (standard OpenTelemetry env vars):

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_SERVICE_NAME=loxo-llm-router          # optional; this is the default
```

`GET /health` reports the tracing config in force under `"otel"` (`enabled`,
`endpoint`, `service_name`). That config resolves once at startup, so an env
edit needs a restart before `/health` — or the exporter — reflects it. Unset the
endpoint (and `LOXO_OTEL_ENABLED`) for zero spans and zero overhead.

### Local Jaeger (all-in-one)

```yaml
# docker-compose.yml
services:
  jaeger:
    image: jaegertracing/all-in-one:latest
    ports:
      - "16686:16686"   # UI
      - "4318:4318"     # OTLP/HTTP
```

```bash
docker compose up -d
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
# run loxo, send a request, then open http://localhost:16686
```

### LangSmith (OTLP ingest)

LangSmith ingests OTLP directly — no proprietary SDK. Auth rides in via the
standard headers env var:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=https://api.smith.langchain.com/otel
export OTEL_EXPORTER_OTLP_HEADERS="x-api-key=<LANGSMITH_API_KEY>"
```

Add `,Langsmith-Project=<name>` to the headers to land spans in a project other
than the default. Regional and self-hosted installs swap the host (`eu.`,
`apac.`, `aws.` prefixes; self-hosted appends `/api/v1` before `/otel`).

Give the **base** URL above, not `.../otel/v1/traces` — the OTLP/HTTP exporter
appends the `/v1/traces` signal path itself. LangSmith's own docs hedge on this
because some collectors don't; ours does.

> Verified against LangSmith 2026-07-27: endpoint path, `x-api-key` header, and
> the exporter's appended signal path all confirmed by a live export (a wrong
> key returns `403 Forbidden`, a right one exports silently). Re-check if it
> ever goes quiet — an export failure is deliberately non-fatal, so a rejected
> key looks exactly like an idle router: healthy `/health`, no traces.
