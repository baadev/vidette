# CLAUDE.md — guide for AI agents working on Vidette

Vidette is a self-hosted video security platform: universal NVR core + a cascaded AI pipeline
that turns camera streams into *understood events* ("someone is trying the door handle"), with
plain-language alert policies as the north star. Current stage: **M0 design preview** — docs
and an executable shell; see [ROADMAP.md](ROADMAP.md) for what exists vs. what is designed.

## Prime directives

1. **The three budgets are the review lens.** Every change is judged on compute, storage and
   latency impact. If a change regresses a budget, say so explicitly in the PR/summary — do not
   hide it. Budgets per milestone live in [ROADMAP.md](ROADMAP.md);
   philosophy in [docs/project/principles.md](docs/project/principles.md).
2. **Recording is sacred.** Never introduce a code path where analysis, notifications or UI
   work can stall or crash the recorder. Load shedding order: Tier 3 → Tier 1–2 → previews →
   never the recorder.
3. **Honesty is a feature.** This repo uses a status legend (✅ 🚧 📐 🔭 ❌ — defined in
   ROADMAP.md). Never present designed functionality as working: not in docs, not in the UI,
   not in API responses (unimplemented endpoints return `501` with `{"status": "designed",
   "milestone": ...}`). Never invent benchmark numbers — design targets must be labeled as
   targets.
4. **Privacy promises are inviolable.** No telemetry, no phone-home, no cloud calls unless the
   user explicitly configured that destination. Identity features are limited to the
   documented opt-in, local-only trusted-faces *suppression* (M4) — no stranger
   identification, no third-party identity databases, no cloud biometrics, and uncertain
   matches never suppress. Do not add "just a version check" HTTP calls.
5. **Security defaults do not weaken.** No default credentials, auth on by default, webhooks
   signed, secrets never logged, config files never contain bootstrap passwords. See
   [docs/architecture/security-model.md](docs/architecture/security-model.md).

## Repository map

| Path | What lives here |
|---|---|
| `server/vidette/core/` | Config schema (pydantic, the executable spec) and event bus/models |
| `server/vidette/db/` | SQLite store (WAL, append-only migrations, single-writer discipline) |
| `server/vidette/auth/` | scrypt hashing, sessions, scoped tokens, FastAPI deps |
| `server/vidette/streams/` | go2rtc manager: config generation, health, WHEP/snapshot proxy client |
| `server/vidette/adapters/` | Camera adapter SDK + adapters (rtsp ✅; Eufy has **no** adapter — it rides `rtsp` via NAS (RTSP), see docs/cameras/eufy.md) |
| `server/vidette/pipeline/` | Cascade tier protocols and (future M2) orchestrator |
| `server/vidette/recording/` | Recorder (ffmpeg supervision), segments, exporter, janitor, retention planner |
| `server/vidette/notify/` | Notifier protocol, webhook HMAC signing |
| `server/vidette/api/` | FastAPI app + routers (auth/cameras/recordings/streams/system); 501 stubs for M2+ |
| `server/vidette/runtime.py` | AppRuntime: boots/stops every subsystem in order (lifespan) |
| `server/tests/` | Pytest suite — includes the test that keeps `config.example.yaml` valid |
| `web/` | React + Vite + TS shell (dark theme, status page) |
| `deploy/` | Dockerfile, compose stack, annotated `config.example.yaml`, go2rtc example |
| `docs/architecture/` | Overview, AI pipeline, storage, security model, plugins + ADRs |
| `docs/cameras/` | Per-vendor adapter docs (eufy.md is the flagship) |
| `docs/project/` | Engineering principles (the public constitution) |
| `internal/` | Maintainer-local docs — gitignored, never commit or quote in public |

## Commands

```bash
make setup          # uv sync (server) + npm install (web)
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

- **New camera adapter:** implement the protocol in `server/vidette/adapters/base.py`,
  register the entry point in `server/pyproject.toml`, add `docs/cameras/<vendor>.md` from
  `docs/cameras/template.md`, add a row to the support matrix, add probe/config tests.
  Non-Python ecosystems use the sidecar-bridge pattern
  ([docs/architecture/plugins.md](docs/architecture/plugins.md)).
- **New pipeline tier/model:** respect the `CascadeBudget`; models must have permissive
  licenses (Apache-2.0/MIT — **no AGPL model code**, see ADR-0003); update
  `docs/architecture/ai-pipeline.md` candidates table.
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
