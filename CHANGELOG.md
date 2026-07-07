# Changelog

All notable changes to Vidette are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/) (pre-1.0: minor bumps may break).

## [Unreleased]

### Added
- Project genesis (M0): architecture documentation and ADRs, selling README, roadmap with
  status legend, contribution/security/Claude guides, growth strategy and brand docs.
- Executable configuration schema (pydantic) with `${ENV}` interpolation, validator CLI
  (`vidette validate`) and API endpoint (`POST /api/v1/config/validate`), covered by tests.
- Typed protocols for camera adapters, pipeline tiers and notifiers; in-process event bus;
  retention planner (pure, tested); HMAC webhook signing (tested).
- FastAPI skeleton: `/healthz`, `/api/v1/system`, honest `501 designed` stubs for M1/M2 surface.
- Web app shell (React + Vite, dark theme) with live health status.
- Docker Compose stack: vidette + go2rtc sidecar, optional `vlm` profile (Ollama).
- CI: ruff, mypy (strict), pytest; web typecheck and build.
