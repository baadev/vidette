# ADR-0001: Python core + go2rtc sidecar + TypeScript web (hybrid runtime)

- **Status:** accepted
- **Date:** 2026-07-07

## Context

Vidette needs: heavy CV/ML integration (detectors, trackers, VLMs — new models monthly),
high-throughput media plumbing, a contributor pool for an open-source project, and a solo
maintainer's velocity. The three budgets (compute/storage/latency) rule the decision — but
crucially, **the hot paths are not application code**: video is encoded by cameras, moved by
a gateway, written by FFmpeg, inferred by C++/CUDA runtimes.

## Decision

- **Core services in Python ≥ 3.12** (FastAPI, pydantic, asyncio): orchestration, adapters,
  event engine, API. Inference via ONNX Runtime / llama.cpp bindings; media via FFmpeg
  subprocesses. Process pools where the GIL would bite.
- **Stream transport in Go — by adoption, not authorship**: go2rtc as a sidecar (ADR-0002).
- **Web in TypeScript + React** (ADR-0007).
- The deliverable is a **modular monolith in one container** plus sidecars — not microservices.

## Consequences

- ✅ Every CV/VLM library lands in Python first; adapter/model contributions have the widest
  possible contributor pool; iteration speed is maximal where the product risk is (the cascade).
- ✅ Hot paths (decode, mux, inference, streaming) run in C/C++/Go regardless of glue language.
- ⚠️ Python orchestration can become a bottleneck at high camera counts — the tripwire is
  profiling showing > 10 % of a core per camera in glue code; the response is process-sharding
  and, only then, rewriting the specific hot module (Rust bindings), never a big-bang rewrite.
- ⚠️ Two toolchains (uv + npm) — accepted; Makefile and CI keep it one command.

## Alternatives considered

- **Rust core** — best raw efficiency; loses on ML-ecosystem lag and contributor pool; solo
  velocity risk. Kept as targeted-module escape hatch.
- **Go core** — great services story; ML inference bindings are second-class; would end up
  shelling out to Python for models anyway.
- **Node/TS core** (Scrypted's path) — one language with the web, but weakest inference story
  and the pipeline is the product.
- **Microservices** — operational tax on a home server for zero benefit at this scale.
