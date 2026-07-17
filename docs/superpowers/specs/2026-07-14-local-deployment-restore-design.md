# Restore the live loxo-llm-router deployment (Mac) — design

**Date:** 2026-07-14
**Status:** approved, not yet implemented
**Success criterion (user-stated):** OpenCode routes through the router again.

> Temporary: this file lives under `docs/superpowers/`, which is gitignored and
> excluded from the public tree. It is force-added deliberately and must be
> `git rm`'d before PR #1 merges. History retention is fine; a tracked file on
> `main` is not.

## Context

The router has been down since its LaunchAgent was stopped (`launchctl bootout
com.local.llm-router`). Three blockers were carried in notes as separate issues;
investigation on 2026-07-14 showed two of them are one problem and one is a bug.

### Blocker 1 — config directory migrated in code, never on disk

`d143598` and `llm-router-serve.sh` moved every path to
`~/.config/loxo-llm-router/`:

| What | Code expects | On disk today |
|---|---|---|
| env file | `~/.config/loxo-llm-router/env` | `~/.config/llm-router/env` |
| spend ledger | `~/.config/loxo-llm-router/spend.jsonl` | `~/.config/llm-router/spend.jsonl` (41 KB) |
| `loxo.toml` | `~/.config/loxo-llm-router/loxo.toml` | absent (bundled defaults apply) |

This was recorded as "env file path mid-migration". It is really a whole-directory
migration; a single `mv` resolves env and ledger together.

### Blocker 2 — `--app-dir` resolves to `~/bin` (bug in a shipped file)

`llm-router-serve.sh` derives its own directory from `${BASH_SOURCE[0]}`:

```bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
```

The plist invokes it through the symlink `~/bin/llm-router-serve.sh`. Bash does
not resolve symlinks in `BASH_SOURCE`, so `SCRIPT_DIR` becomes `/Users/brendanl/bin`,
which contains no `loxo_llm_router` package — uvicorn fails to import the app.
The comment above the line reads "no hardcoded paths", which is true and is
precisely why it breaks.

`LOG_CONFIG` resolves off the same variable, so logging config silently
evaporates through the same symlink with no error. Recorded as "package not in
app-dir"; it is a bug, not deployment drift.

### Blocker 3 — namespace mismatch with OpenCode

`config.py:81`: `ns = os.environ.get("ROUTER_NS") or data.get("namespace", "loxo")`.
Bundled default is `loxo`; OpenCode still asks for `airwolf/*` across 6 files.

## Decisions

1. **Finish the rebrand to `loxo`** rather than pinning `ROUTER_NS=airwolf`. The
   half-migrated state is itself the recurring bug generator — it has produced
   two wrong claims in notes already. Fallback stays one line
   (`ROUTER_NS=airwolf`) if anything misbehaves.
2. **No `loxo.toml`.** The bundled `loxo.default.toml` already matches the intended
   cost-conscious tier map (`auto`/`balanced` → `z-ai/glm-5.2`, `fast` →
   `z-ai/glm-4.7-flash`, `reason` → `moonshotai/kimi-k2.6`, `deep` →
   `google/gemini-2.5-pro`). Creating one would only add drift surface.
3. **No Docker.** Docker is not installed; a container buys nothing for a single
   local always-on service. `docker-compose.yml` remains the deploy story for
   other machines.
4. **Local mlx backend is in scope**, via copying the model to internal SSD.

## Design

### A. Config directory

`mv ~/.config/llm-router ~/.config/loxo-llm-router` — relocates env file and
`spend.jsonl` together. `mv` preserves the ledger, so `/v1/spend` totals stay
continuous.

### B. Serve script symlink resolution — repo change

```bash
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
```

`readlink -f` verified working on this machine (Darwin 25.4); resolves the `~/bin`
symlink to the repo. Sets a floor of macOS 12.3+ / any modern Linux.

`llm-router-serve.sh` is tracked, so this is a **repo commit on `public-release`**
that lands in PR #1, not local config.

### C. Router env additions

Add to `~/.config/loxo-llm-router/env`:

```
LOCAL_MODELS=mlx-community/Qwen3.6-35B-A3B-4bit
```

`local_models` defaults to `[]`, and `[tiers.local]` sets no `local_target`, so
`local_target_for()` falls through to the caller's model id and would send the
literal string `loxo/local` to mlx. Existing keys (`OPENROUTER_API_KEY`,
`VISION_*`) are unchanged by the move.

### D. Local mlx backend

Symptom, per `docs/mlx-vlm-launchagent-debug.md` (deleted in `02e7981`,
recovered from history at `02e7981^`): under launchd, **any filesystem operation
on the external Sunburst volume blocks indefinitely**, confirmed via
`faulthandler`. `HF_HUB_OFFLINE=1`, `HF_TOKEN_PATH=/dev/null`, `taskpolicy -c
user-interactive`, and `LowPriorityIO=false` all failed. The same script runs
fine from a shell.

**Root cause — corrected 2026-07-14.** Those notes concluded "the I/O scheduler
throttles all access to the external volume under launchd regardless of QoS
class." That is very likely **wrong**. The learnings store (`~/src/learnings`)
holds a 2026-07-11 finding — three weeks *later* than the mlx notes, never
connected back — that external-volume reads hanging in terminal/SSH but working
in Finder are **macOS TCC Full Disk Access**, not hardware and not throttling.
The launchd case is the same mechanism: a launchd-spawned process does not
inherit the terminal's FDA grant and cannot show a consent prompt, so it blocks
forever. Throttling would make I/O *slow*, not *infinitely blocked*; and the
failure of `taskpolicy`/`LowPriorityIO` is exactly what you would expect if
priority was never the mechanism. Captured as
`2026-07-14-launchd-jobs-lack-full-disk-access-...` in the store.

**This does not change the plan.** Moving the model to internal SSD sidesteps
TCC entirely, needs no privacy grant, and loads faster. Granting FDA instead
would mean granting it to `/bin/bash` (the responsible process for a shell-script
LaunchAgent) — not worth it. But note the copy **cannot discriminate between the
two explanations**: both predict success once the model is local. Do not treat a
working LaunchAgent afterwards as confirmation of the TCC theory.

State on 2026-07-14:

- Sunburst mounted, holds the full 19 GB model.
- `~/.cache/huggingface/hub/models--mlx-community--Qwen3.6-35B-A3B-4bit` exists but
  is an **abandoned download from 2026-04-19**: 436 MB of 19 GB, 4 `.incomplete`
  blobs. Not usable; must be cleared before copying or `huggingface_hub` will trip
  over it.
- Internal SSD has 212 GB free; a 19 GB copy leaves ~193 GB.
- HF snapshot symlinks are **relative** (`../../blobs/<hash>`), verified — so `cp -a`
  yields a self-contained local copy with no residual Sunburst references. Had they
  been absolute, the copy would not have fixed the hang.

Steps:

1. `rm -rf ~/.cache/huggingface/hub/models--mlx-community--Qwen3.6-35B-A3B-4bit`
2. `cp -a /Volumes/Sunburst/hf/hub/models--mlx-community--Qwen3.6-35B-A3B-4bit ~/.cache/huggingface/hub/`
3. In `~/bin/mlx-vlm-serve.sh`: `HF_HOME=/Volumes/Sunburst/hf` → `HF_HOME=/Users/brendanl/.cache/huggingface`.
   `HF_HUB_OFFLINE=1` is already set, so the `token` file that previously blocked is never read.
4. Verify from a shell: server loads (~35 s), answers on `:7979`.
5. **Then** `launchctl bootstrap` `com.local.mlx-vlm.plist` and verify it works
   *under launchd*. This is the real experiment — launchd is what has always
   failed, so a shell-only test proves nothing new.

### E. OpenCode rebrand — 6 files

- `~/.config/opencode/opencode-openrouter-and-local.json`: provider key
  `airwolf-llm-router` → `loxo-llm-router`; 6 model ids `airwolf/*` → `loxo/*`.
  `baseURL` stays `http://localhost:9090/v1` (matches bundled default port).
- `agents/{architect,balanced,plan-executor,reason,local}.md`: frontmatter
  `model: airwolf-llm-router/airwolf/<tier>` → `loxo-llm-router/loxo/<tier>`.
- `agents/local.md`: prose references to `airwolf/auto`, `airwolf/deep`.

Risk: agent pins fail **silently** — a wrong id does not error, the agent quietly
inherits the session model. Verify each pin resolves rather than assuming.

### F. LaunchAgent

No plist edit. `com.local.llm-router.plist` points at the `~/bin` symlink, which
is correct once (B) makes the script resolve itself.

**Tahoe gotcha.** This machine is macOS 26.4.1. Per a 2026-07-12 learning in the
store, `launchctl bootstrap gui/$(id -u) <plist>` on Tahoe can fail with "Domain
does not support specified action" (and `user/$(id -u)` fails with an I/O error
even under sudo). This is a Tahoe launchd change, not a plist error. `bootout`
works fine — the two are not symmetric, which is why stopping the agent earlier
was clean. Workarounds, in order: reboot/re-login (Tahoe auto-loads
`~/Library/LaunchAgents/*.plist`), else move the plist to `/Library/LaunchAgents`
owned `root:wheel` and bootstrap from there. Applies to both the router agent and
the mlx agent.

## Execution order

1. Move config dir (A)
2. Fix serve script (B) — commit to repo
3. Add `LOCAL_MODELS` (C)
4. mlx: clear partial → copy → repoint `HF_HOME` → shell verify → launchd verify (D)
5. Bootstrap router LaunchAgent (F)
6. Verify router (below)
7. Rebrand OpenCode (E)
8. Verify OpenCode end-to-end

Router before OpenCode throughout: confirm the server is healthy before changing
the client, so a failure has one candidate cause instead of two.

## Verification

| Step | Check | Expected |
|---|---|---|
| 1 | `curl :9090/v1/models` | six `loxo/*` ids |
| 2 | `curl` completion via `loxo/fast` | real response — proves `OPENROUTER_API_KEY` loaded |
| 3 | `curl :9090/v1/spend` | prior totals, not zero — proves ledger survived the move |
| 4 | `curl` completion via `loxo/local` | served by mlx on `:7979` |
| 5 | mlx under launchd | serves without hanging — the actual open question |
| 6 | OpenCode | a cloud tier and `loxo/local` both answer |

## Rollback

| Change | Revert |
|---|---|
| namespace | `ROUTER_NS=airwolf` in env file — one line, undoes nothing else |
| config dir | `mv` back |
| `HF_HOME` | point back at Sunburst; `bootout` the mlx plist |
| serve script | git revert |

Sunburst is only ever read, never modified.

## Known gaps and uncertainty

- **The launchd hang's cause is corrected but unverified** (see D). TCC/Full Disk
  Access is strongly supported over the notes' I/O-throttling claim, but neither
  is proven, and the copy cannot discriminate. If launchd still hangs with the
  model local, that is genuinely new information — fall back to manual `nohup`
  start rather than cycling guesses.
- **Consult `~/src/learnings` before designing, not after.** Both the TCC
  correction and the Tahoe `bootstrap` failure were already in the store and were
  missed on the first pass of this spec, which is exactly the re-derivation the
  store exists to prevent.
- **Vision shim unverified.** `VISION_*` env vars reference a VLM; the working mlx
  command serves text-only `mlx_lm`. `[tiers.local]` sets `vision = "local"`.
  Whether vision works locally is untested and out of scope.
- **`auto` tier degrades quietly when local is down**: tries local for prompts
  under 60k tokens, connection fails, `cloud_fallback_for()` transparently reroutes
  to cloud. Correct by design, invisible in practice. Not changed here.
- The serve script and log config are still named `llm-router-*`, not `loxo-*`.
  Renaming would break the `~/bin` symlink and the plist. Out of scope.
