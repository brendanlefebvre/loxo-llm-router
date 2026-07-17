# loxo-llm-router

An OpenAI-compatible router that dispatches each request to a **local** (MLX,
Ollama, llama.cpp, vLLM, LM Studio — anything OpenAI-compatible) or **cloud**
(OpenRouter) backend based on **intent-expressing heuristics**, not
retry-on-failure. Clients send one virtual model id; the router resolves it to
the right backend and model.

```
clients ──▶ loxo-llm-router (:9090/v1) ──┬──▶ local model server (:7979/v1)
                                         └──▶ OpenRouter (cloud)
```

How it works inside — routing rules, config layering, tiers, the failure
philosophy — is documented in [ARCHITECTURE.md](ARCHITECTURE.md). Where it's
headed is in [ROADMAP.md](ROADMAP.md).

## Install & run

```bash
pip install .                 # Python >= 3.11
export OPENROUTER_API_KEY=sk-or-...
loxo-llm-router               # serves on 0.0.0.0:9090 with bundled defaults
```

Docker:

```bash
cp .env.example loxo.env      # add your OPENROUTER_API_KEY
docker compose up --build
```

## Configure

Configuration layers, lowest to highest precedence:

1. **bundled defaults** — runs out of the box
2. **`loxo.toml`** — your tier catalog + app settings (copy `loxo.toml.example`)
3. **environment** — overrides scalars, and is the **only** place for secrets

Config-file search order: `$LOXO_CONFIG` → `./loxo.toml` →
`~/.config/loxo-llm-router/loxo.toml`. See `loxo.toml.example` for the full
schema and `.env.example` for environment options.

**Secrets** (`OPENROUTER_API_KEY`, `ROUTER_TOKEN`) are read only from the
environment — never put them in `loxo.toml`.

**Host/port** resolve `LOXO_HOST`/`LOXO_PORT` → bare `HOST`/`PORT` → `[server]`
in `loxo.toml` → default `0.0.0.0:9090`. The bare `PORT` is honored so platforms
that inject it (Heroku, Cloud Run, Railway) work with no extra config; set
`LOXO_PORT` to force a value regardless of any platform-set `PORT`. Docker is the
default deployment — a container's environment is hermetic, so the bare names are
safe there.

## Tiers

Tiers are defined in `[tiers.*]`; each becomes the virtual id
`<namespace>/<key>`. Defaults:

| Tier | Routing | Vision |
|---|---|---|
| `loxo/auto` | local-first; escalate on size / `x-quality: best` | shim |
| `loxo/fast` | pinned cloud | reject images (422) |
| `loxo/balanced` | pinned cloud | shim |
| `loxo/reason` | pinned cloud | native |
| `loxo/deep` | pinned cloud | native |
| `loxo/local` | pinned local, hard-fail (no cloud fallback) | local OCR only |

Rebrand the whole namespace by setting `namespace` in `loxo.toml` (or
`ROUTER_NS`). Add/retarget tiers by editing the `[tiers.*]` table.

## Routing rules (first match wins)

1. `model` matches a tier id → resolve by that tier's policy
2. `model` matches a `local_models` entry → local
3. `x-quality: best` header → cloud
4. estimated prompt > `local_context_limit` → cloud
5. `model` contains `/` (provider-prefixed) → cloud
6. default → local

Plus a fallback: if local is chosen but unreachable (or disconnects
mid-stream), forward to cloud with `cloud_default_model` — except for the
hard-fail `loxo/local` tier.

## Deploying

See [docs/deploy.md](docs/deploy.md) for Docker, systemd (Linux), and launchd
(macOS) recipes.

## License

MIT — see [LICENSE](LICENSE).
