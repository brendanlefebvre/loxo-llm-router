# loxo-llm-router

Your coding agent fires hundreds of LLM calls a session — the actual work, but
also titles, summaries, compaction, tool round-trips — and pays cloud rates for
every one, with no line-item of what went where. **loxo** sits in front as a single
OpenAI-compatible endpoint and routes each call to a **local** model or the
**cloud** by intent: you pick a tier (or let `loxo/auto` stay local until the
prompt gets big or you ask for the best), and every cloud dollar is tracked per
model.

```text
clients ──▶ loxo-llm-router (:9090/v1) ──┬──▶ local model server (:7979/v1)
                                         └──▶ OpenRouter (cloud)
```

Works with any OpenAI-compatible client — OpenCode, Pi, the OpenAI SDKs. Point
it at loxo, send one virtual model id (e.g. `loxo/auto`), and loxo resolves it
to the right backend and model. On the local side, that backend can be anything
OpenAI-compatible: MLX, Ollama, llama.cpp, vLLM, LM Studio.

> Today (v0.1.0) loxo routes by **declared intent** — the tier you pick, the
> prompt size, an `x-loxo-quality` header. Teaching it to shift work onto local
> models by *measured* adequacy — the local/frontier "dial" — is the
> [ROADMAP](ROADMAP.md).

## Who this is for

loxo's cost story is real for some operators and honestly not for others:

- **Pay-per-token users** — you meter API spend today (OpenRouter, direct API
  keys). Routing chore traffic local and tracking every cloud dollar attacks
  the bill you actually pay.
- **Privacy / local-first users** — you want work kept on-device wherever
  adequacy allows, at any price.
- **Multi-model routers** — you already mix models per task and want that
  selection measured instead of vibes-based.

**Not a cost win:** heavy users on subsidized flat-rate subscriptions
(Claude Max class). Per-token routing cannot beat a subsidized subscription
by arithmetic — if that's your baseline, loxo earns its keep only on the
privacy and measurement axes, not price.

Near-term savings concentrate in the **chore traffic** (titles, summaries,
compaction); shifting main-turn work local is the hard, later win — see the
[ROADMAP](ROADMAP.md).

## Quickstart

Install and run:

```bash
pip install .                 # Python >= 3.11
export OPENROUTER_API_KEY=sk-or-...
loxo-llm-router               # serves on 0.0.0.0:9090 with bundled defaults
```

Or with Docker:

```bash
cp .env.example loxo.env      # add your OPENROUTER_API_KEY
docker compose up --build
```

Send it a request — point any OpenAI-compatible client at `http://localhost:9090/v1`
and use a `loxo/*` model id:

```bash
curl http://localhost:9090/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model": "loxo/auto", "messages": [{"role": "user", "content": "hello"}]}'
```

Then see what it cost:

```bash
curl http://localhost:9090/v1/spend    # real USD, per provider and model
```

To use loxo from an agent, set the tool's base URL to `http://localhost:9090/v1`
and its model to a `loxo/*` tier.

## Tiers

A **tier** is a virtual model id (`<namespace>/<key>`, default namespace `loxo`)
that stands in for a routing policy. Clients only ever send the tier; loxo
resolves it to a real backend and model. The bundled tiers:

| Tier | Routing | Vision |
|---|---|---|
| `loxo/auto` | local-first; escalate on size / `x-loxo-quality: best` | shim |
| `loxo/fast` | pinned cloud | reject images (422) |
| `loxo/balanced` | pinned cloud | shim |
| `loxo/reason` | pinned cloud | native |
| `loxo/deep` | pinned cloud | native |
| `loxo/local` | pinned local, hard-fail (no cloud fallback) | local OCR only |

Add or retarget tiers by editing the `[tiers.*]` table in `loxo.toml`.

## Configure

Configuration resolves in three layers, lowest to highest precedence:

1. **bundled defaults** — loxo runs out of the box with none of the below
2. **`loxo.toml`** — your tier catalog and app settings (copy `loxo.toml.example`)
3. **environment** — overrides scalars, and is the **only** place for secrets

Secrets (`OPENROUTER_API_KEY`, `ROUTER_TOKEN`) are read **only** from the
environment — never put them in `loxo.toml`. See `loxo.toml.example` and
`.env.example` for the full schema, and [ARCHITECTURE.md](ARCHITECTURE.md) for
the config search order, host/port resolution, and routing rules.

## Learn more

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — how routing, config layering, tiers,
  vision, spend tracking, and the failure philosophy actually work.
- **[ROADMAP.md](ROADMAP.md)** — where loxo is headed: the local/frontier dial.
- **[docs/deploy.md](docs/deploy.md)** — Docker, systemd (Linux), and launchd
  (macOS) deployment recipes.

## License

MIT — see [LICENSE](LICENSE).
