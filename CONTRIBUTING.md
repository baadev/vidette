# Contributing to Vidette

Thank you for considering it. At the M0 stage, **design review is the most valuable
contribution**: read [docs/architecture](docs/architecture/overview.md) and tell us where the
design is wrong — in an issue, an ADR comment, or by email (alex@baadev.com).

## Ways to contribute (ordered by current impact)

1. **Critique the architecture.** Especially the [AI pipeline](docs/architecture/ai-pipeline.md),
   [storage](docs/architecture/storage.md) and [plugin](docs/architecture/plugins.md) designs.
2. **Map camera demand.** File a
   [camera support request](https://github.com/baadev/vidette/issues/new?template=camera_support.yml) —
   these order the adapter backlog. If you can capture RTSP URLs, ONVIF traces or vendor API
   notes for your camera, that's gold.
3. **Docs fixes.** Unclear sentence = bug. PRs welcome without prior discussion.
4. **Code.** Check issues labeled `good first issue` / `help wanted`. For anything larger than
   a small fix, open an issue first so nobody wastes an evening.

## Development setup

Prerequisites: Python ≥ 3.12 with [uv](https://docs.astral.sh/uv/), Node ≥ 20, Docker
(optional, for the compose stack).

```bash
git clone https://github.com/baadev/vidette.git && cd vidette
make setup     # server deps (uv) + web deps (npm)
make test      # pytest
make lint      # ruff + mypy strict + web typecheck
make dev       # API on http://localhost:8642
make web       # web shell on http://localhost:5173 (proxies /api)
```

## Pull request rules

- **Small and focused** beats large and heroic. One concern per PR.
- **Conventional Commit** titles: `feat: ...`, `fix: ...`, `docs: ...`, `refactor: ...`,
  `test: ...`, `chore: ...`.
- **DCO sign-off required** (`git commit -s`). By signing off you certify the
  [Developer Certificate of Origin](https://developercertificate.org/) — that you have the
  right to submit the code under Apache-2.0. No CLA.
- **Tests and docs land with code.** Feature PRs update ROADMAP.md status, the relevant
  docs page and CHANGELOG.md. Pure logic requires unit tests.
- **CI must be green**: ruff, mypy (strict), pytest, web typecheck/build.
- **Budgets:** if your change plausibly affects compute, storage or latency, state the impact
  in the PR description (measured if you can, reasoned if you can't).
- Significant design choices need an [ADR](docs/architecture/adr/) — copy the template, open
  it as part of the PR.

## Code style

Enforced by tools, not opinions: `ruff format` + `ruff check` + `mypy --strict` for Python,
`tsc --noEmit` strict for TypeScript. See [CLAUDE.md](CLAUDE.md) for the conventions AI agents
follow — humans follow the same ones.

## Non-negotiables (will not be merged)

- Telemetry, phone-home, or cloud calls outside explicitly configured destinations.
- Weakened security defaults (default creds, unauthenticated surfaces, unsigned webhooks).
- Stranger identification, third-party identity databases, or cloud biometrics — identity is
  limited to opt-in, local-only
  [trusted-faces suppression](docs/faq.md#what-about-face-recognition).
- AGPL-licensed dependencies in the default install (ADR-0006).
- Vendor credentials, decryption keys or proprietary blobs — adapters integrate with
  *community* bridge projects; we don't ship what isn't ours to ship.

## Communication

- Bugs & features: [GitHub issues](https://github.com/baadev/vidette/issues)
- Design & questions: GitHub Discussions (once enabled) or issues
- Direct: **alex@baadev.com**
- Security vulnerabilities: **not** in public issues — see [SECURITY.md](SECURITY.md)

## Governance & releases

Vidette is currently maintained by Alexander Belov (BDFL-for-now); a broader maintainer model
is documented intent once there are regular contributors. Releases follow SemVer (pre-1.0:
minor may break, patch never does) with notes in CHANGELOG.md. Contributors are credited in
release notes — generously.

## Code of conduct

Participation implies the [Code of Conduct](CODE_OF_CONDUCT.md). TL;DR: be the kind of
reviewer you'd want at 2 a.m. when your build is red.
