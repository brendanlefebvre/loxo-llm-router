# MLX-VLM LaunchAgent Startup Hang — Debug Notes

**Date:** 2026-06-22  
**Status:** Unresolved; parked for later.

## Symptom

`com.local.mlx-vlm` LaunchAgent starts but hangs forever at:

```
INFO:     Waiting for application startup.
2026-06-22 13:29:27,032 - INFO - Pre-loading model: mlx-community/Qwen3.6-35B-A3B-4bit
```

The process runs (~420 MB RSS), consumes 0% CPU, opens no model files, and never
completes startup. Port 7979 never opens.

## What We Ruled Out

- **Wrong model in script** — the script previously had `gemma-4-12B-it-6bit`;
  corrected to `Qwen3.6-35B-A3B-4bit` (matches router's `LOCAL_MODELS`). Not the
  root cause.
- **Sunburst volume** — mounted and healthy (528 GB used / 1.8 TB, 29% capacity).
- **Python interpreter** — `ps` shows the Homebrew Python path, but that's just
  symlink resolution; the venv Python IS the Homebrew Python. Not the root cause.
- **Output buffering** — added `PYTHONUNBUFFERED=1` and `-u` to the exec line.
  Gives more log output, but doesn't fix the hang.
- **Model files** — `mlx-vlm-test-load.sh` (`~/bin/`) loaded the model cleanly in
  ~25 s using `mlx_vlm.load()` directly. Files are present and uncorrupted.
- **Memory** — 11 GB free RAM; plenty. Not the root cause.

## Key Clue

When started manually with stdout/stderr redirected to files AND `-u`:

```bash
HF_HOME=/Volumes/Sunburst/hf \
  /Users/brendanl/.venvs/mlx/bin/python3 -u -m mlx_vlm.server \
  --model mlx-community/Qwen3.6-35B-A3B-4bit --host 0.0.0.0 --port 7979 \
  >>~/Library/Logs/mlx-vlm/manual-out.log 2>>~/Library/Logs/mlx-vlm/manual-err.log &
```

…it **loaded successfully in ~25 seconds** and the server came up. The LaunchAgent
runs the same script with the same redirects (StandardOutPath / StandardErrorPath),
yet hangs. Something about the LaunchAgent execution context differs.

## Leading Hypothesis

`mlx_vlm.server` uses uvicorn, which forks a worker subprocess for the actual
startup:

```
INFO:     Started server process [XXXX]   ← child PID, different from the parent
INFO:     Waiting for application startup.
INFO:     Pre-loading model: ...
```

The model load happens inside that child process. Under LaunchAgent the child
may be hitting a macOS sandbox restriction, entitlement issue, or process-group
signal isolation that blocks it. The child silently fails and the parent loops
waiting for it.

**Things to try next:**

1. Check whether the child process actually spawns: add a `ps aux | grep mlx_vlm`
   probe right after startup to see if there's a second PID with high CPU doing
   the load.  Under LaunchAgent we only ever saw ONE process at low CPU; the
   manual run produced TWO (one parent + one high-CPU loader).
2. Add `--workers 1` or uvicorn-level flags to prevent subprocess forking.
3. Try `launchctl debug` or `launchctl print` to inspect sandbox restrictions on
   the agent.
4. Try switching to `mlx_lm.server` (plain text server, not VLM) for the primary
   text model, since `mlx_vlm.server` is a VLM server being used for non-VLM text
   inference anyway. The vision OCR path already has a separate model loaded on
   demand.

## Files Changed During Debugging

| File | Change |
|------|--------|
| `~/bin/mlx-vlm-serve.sh` | Fixed model name; added `PYTHONUNBUFFERED=1`, `-u`, `--log-level DEBUG`, startup echo lines |
| `~/bin/mlx-vlm-debug.sh` | New — real-time log monitor highlighting key events |
| `~/bin/mlx-vlm-test-load.sh` | New — standalone model load test (no server) |

## Diagnostics Added to Serve Script

- Timestamped banners at each startup phase (volume wait, env dump, disk space)
- `PYTHONUNBUFFERED=1` + `-u` to defeat Python output buffering
- `--log-level DEBUG` on the server for httpx-level network traces
