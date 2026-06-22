# MLX LM/VLM Server — Manual Launch (as of 2026-06-22)

**Status:** LaunchAgent disabled; server started manually when needed.

## Why not the LaunchAgent?

Under `launchctl`, **any filesystem operation on the external Sunburst volume
blocks indefinitely** — `open()`, `os.path.exists()`, `os.scandir()` all hang.
This affects both `mlx_lm.server` and `mlx_vlm.server`. The same scripts run
fine from a shell (`nohup ... &`).

### Root cause (confirmed via `faulthandler`)

1. `huggingface_hub` derives `HF_TOKEN_PATH` from `HF_HOME` →
   `/Volumes/Sunburst/hf/token`. Reading it blocks.
2. Even with that bypassed, `snapshot_download` calls `os.path.exists()` and
   `open()` on the Sunburst-hosted `refs/` and `snapshots/` paths. All block.
3. `/v1/models` calls `scan_cache_dir()` → `os.scandir()` on
   `/Volumes/Sunburst/hf/hub` — same hang.

Setting `HF_HUB_OFFLINE=1`, `HF_TOKEN_PATH=/dev/null`, `PYTHONUNBUFFERED=1`,
`-u`, `taskpolicy -c user-interactive`, and plist `LowPriorityIO=false` did
**not** fix it. The I/O scheduler throttles all access to the external volume
under launchd regardless of QoS class.

### What works

Running the same script from a shell with `nohup` loads the model in ~35s:

```bash
HF_HOME=/Volumes/Sunburst/hf \
  /Users/brendanl/.venvs/mlx/bin/python3 -u -m mlx_lm server \
  --model mlx-community/Qwen3.6-35B-A3B-4bit \
  --host 0.0.0.0 --port 7979 --log-level INFO \
  &
```

Add `--enable-thinking` if you want the reasoning channel populated.

### Why this is acceptable

- The router's transport fallback (5s timeout → cloud) handles local being
  down transparently. No user-facing breakage.
- Cloud spend is tracked either way via `/v1/spend`.
- Starting the server manually takes ~35s; model loads lazily on first request.

## Files (for when this is revisited)

| File | Purpose |
|------|---------|
| `~/bin/mlx-vlm-serve.sh` | LaunchAgent script (kept for reference; currently not loaded) |
| `~/bin/mlx-lm-launch.py` | Python wrapper that blocks interrupting signals (kept) |
| `~/bin/mlx-vlm-test-load.sh` | Standalone model load test (works under LaunchAgent since no I/O on Sunburst) |
| `~/bin/mlx-vlm-debug.sh` | Log monitor (kept) |
| `~/Library/LaunchAgents/com.local.mlx-vlm.plist` | LaunchAgent plist (not loaded) |

## Next steps if revisited

- Copy the 19 GB model to local SSD (`~/.cache/huggingface/`) and point
  `HF_HOME` there. This avoids the external-volume I/O issue entirely.
- Or: create a local-only HF cache and symlink just the model snapshot from
  Sunburst (but symlink resolution may still hit the I/O scheduler).
- Or: run the server under a user-level systemd equivalent that doesn't
  throttle I/O (e.g., a tmux session managed by a shell profile).
