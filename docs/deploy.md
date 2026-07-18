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
router logs a pointer; move the file to the state dir when convenient.

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
