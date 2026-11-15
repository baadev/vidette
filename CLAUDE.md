# CLAUDE.md — guide for AI agents working on Vidette

Vidette is an **intent layer on top of Frigate**: it reads Frigate's review items, judges
them against alert policies written in plain language (VLM verdicts, week 2 of the v0.2
plan), and measures how good those judgments are with `vidette eval`. Current stage: the
`v0.2` development line ([ADR-0009](docs/architecture/adr/0009-intent-layer-over-frigate.md)).
The v0.1 NVR (recording, live view, detection cascade, ONVIF) is frozen at tag `nvr-core`;
do not resurrect it on this line. See [ROADMAP.md](ROADMAP.md) for what exists.

## Prime directives

1. **The three budgets are the review lens.** Every change is judged on compute, storage and
   latency impact. If a change regresses a budget, say so explicitly in the PR/summary — do not
   hide it. Budgets per milestone live in [ROADMAP.md](ROADMAP.md);
   philosophy in [docs/project/principles.md](docs/project/principles.md).
2. **Measure before claiming.** No accuracy or false-alarm statement anywhere without a
   `vidette eval` result on a named dataset, with its size. Workers never crash or stall each
   other: every failure becomes a rate-limited system event. An incident without a verdict
   never notifies.
3. **Honesty is a feature.** This repo uses a status legend (✅ 🚧 📐 🔭 ❌ — defined in
   ROADMAP.md). Never present designed functionality as working: not in docs, not in the UI,
   not in API responses (unimplemented endpoints return `501` with `{"status": "designed",
   "milestone": ...}`). Never invent benchmark numbers — design targets must be labeled as
   targets.
4. **Privacy promises are inviolable.** No telemetry, no phone-home, no cloud calls unless the
   user explicitly configured that destination. Identity features are limited to the
   documented opt-in, local-only trusted faces (known people, ADR-0012) and the opt-in
   anonymous identities of ADR-0014 (unknown faces counted as numbers, retention, off by
   default) — no naming of strangers from any source, no third-party identity databases,
   no cloud biometrics, no appearance attributes (age, gender, skin colour) in matching,
   suspicion or policy, and uncertain matches never suppress. Do not add "just a version check" HTTP calls.
5. **Security defaults do not weaken.** No default credentials, auth on by default, webhooks
   signed, secrets never logged, config files never contain bootstrap passwords. See
   [docs/architecture/security-model.md](docs/architecture/security-model.md).

## Repository map

| Path | What lives here |
|---|---|
| `server/vidette/core/` | Config schema (pydantic, the executable spec), event bus, incident model |
| `server/vidette/frigate/` | Frigate source (MQTT), HTTP client, payload models, `vidette frigate` CLI |
| `server/vidette/events/` | Event intake (incident → observed event), canonical payload |
| `server/vidette/verdicts/` | `Judge` interface and model-free baselines (the VLM judge lands here) |
| `server/vidette/people/` | Known people (ADR-0012): face store, YuNet/SFace engine (opt-in extra), face check, `vidette people`; anonymous identities and the recurrence rule (ADR-0014) |
| `server/vidette/eval/` | `vidette eval`: dataset format, metrics, runner, reports |
| `server/vidette/db/` | SQLite store (WAL, append-only migrations, single-writer discipline) |
| `server/vidette/auth/` | scrypt hashing, sessions, scoped tokens, FastAPI deps |
| `server/vidette/notify/` | Webhooks (HMAC), Apprise, web push, MQTT + HA discovery, dispatcher |
| `server/vidette/telegram/` | The Telegram bot (ADR-0013): Bot API client, routing by policy level, alerts, digest, commands, buttons |
| `server/vidette/label/` | `vidette label`: drafts, contact sheets, review replies; `feedback.py` turns live answers into labels |
| `server/vidette/api/` | FastAPI app + routers (auth/events/metrics/push/system/ws); 501 stubs |
| `server/vidette/runtime.py` | AppRuntime: boots/stops every subsystem in order (lifespan) |
| `server/tests/` | Pytest suite; `fixtures/frigate/` holds recorded-format Frigate sessions |
| `datasets/` | Shareable `vidette eval` sets (synthetic only so far) |
| `web/` | React + Vite + TS: Login, Events, System |
| `deploy/` | Dockerfile, compose (one service), annotated `config.example.yaml` |
| `docs/` | Frigate setup, eval, configuration, API, ADRs; NVR-era pages carry a "frozen" banner |
| `internal/` | Maintainer-local docs — gitignored, never commit or quote in public |

## Commands

```bash
make setup          # uv sync --locked (server) + npm ci (web)
make test           # pytest
make lint           # ruff check + mypy strict + web typecheck
make fmt            # ruff format
make dev            # uvicorn on :8642 with reload
make web            # vite dev server (proxies /api to :8642)
make up             # docker compose stack
```

Without make: `cd server && uv run pytest` (or `pip install -e .[dev]`-equivalent via
`uv sync`), `cd web && npm run typecheck && npm run build`.

## Conventions

- **Python ≥ 3.12.** Ruff (line length 100) + mypy `strict` must pass. Full type hints;
  `Protocol` for boundaries; pydantic v2 for anything user-facing. Async-first; no blocking IO
  in async paths. No bare `except`; errors must tell the user what to do next.
- **TypeScript strict**; keep the web shell dependency-light (React, no UI framework yet).
- **Tests:** pure logic gets unit tests in the same PR. `deploy/config.example.yaml` must
  always parse against the schema — there is a test enforcing this; update both together.
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org)
  (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`) + DCO sign-off (`git commit -s`).
- **Docs land with code.** A feature PR updates ROADMAP status, relevant docs/ pages and
  CHANGELOG. Docs are English, sentence-case headings, mermaid for diagrams.
- **Config keys** are `snake_case`; durations are strings like `3d`, `12h`, `forever`;
  env interpolation is `${VAR}` only.

## Architecture decisions

Significant/irreversible choices go through an ADR in
[docs/architecture/adr/](docs/architecture/adr/) (template provided). Never rewrite an
accepted ADR — supersede it with a new one. Heavy new dependencies (anything with native
code, a daemon, or > ~5 MB) need an ADR or explicit maintainer sign-off.

## Golden paths

- **New judge (e.g. the VLM worker):** implement `vidette.verdicts.base.Judge`; never read
  a dataset's `alert_on` (ground truth); report `vlm_calls` / `images_sent` honestly; run it
  with `vidette eval` against the baselines before wiring it live.
- **New Frigate field or message type:** add it to `vidette/frigate/models.py` leniently
  (optional, defaulted), extend a fixture in `server/tests/fixtures/frigate/`, and say in the
  fixture README where the payload came from.
- **New incident source:** produce `vidette.core.incidents.Incident` on `incident.*`; the
  intake, eval and judges stay unchanged.
- **New notifier:** prefer an Apprise URL scheme over a custom integration; custom ones
  implement the `Notifier` protocol and must support payload templating and redaction.
- **New API surface:** design it in `docs/api.md` first; unimplemented routes ship as `501
  designed` stubs so the API is self-documenting about the roadmap.

## Don'ts

- Don't commit secrets, sample footage with people/plates, or user configs (`config/`,
  `media/` are gitignored — keep it that way).
- Don't edit `LICENSE`/`NOTICE`, past ADRs, or the status legend semantics.
- Don't add cloud dependencies to core paths; cloud is always an opt-in provider behind a
  local-first default.
- Don't "improve" the tone of user-facing docs into marketing fluff. The voice is calm,
  precise, a little wry; claims are verifiable; status marks are accurate.

## Definition of done

Code typed + linted + tested; budgets stated if touched; docs + ROADMAP + CHANGELOG updated;
CI green; no new warnings from `vidette validate` on the example config; PR description says
what was verified and how.
