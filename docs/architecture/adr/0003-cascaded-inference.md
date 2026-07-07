# ADR-0003: Cascaded inference (T0–T4) as the core AI architecture

- **Status:** accepted
- **Date:** 2026-07-07

## Context

The product promise is semantic ("alert when someone seems interested in entering") but the
compute budget is a $150 mini-PC. VLM-on-everything violates the compute budget by ~4 orders
of magnitude; detector-only cannot answer the semantic question; motion-only is the vendor
status quo we exist to replace. Full design: [ai-pipeline.md](../ai-pipeline.md).

## Decision

A five-tier cascade — motion gate → small detector → trajectory geometry → budgeted VLM →
compiled policy — where each tier gates the next, expensive tiers run orders of magnitude
more rarely, and the system **degrades to the deterministic tiers** under load or when no
VLM is configured. Detection models: permissive licenses only (Apache-2.0/MIT), executed via
ONNX Runtime with per-hardware execution providers. The VLM is a *judge with a budget*, never
the hot path; alerts ship at T2 speed and are enriched asynchronously.

## Consequences

- ✅ Semantic answers at deterministic-system cost; runs meaningfully on CPU-only hardware.
- ✅ Explainability: every alert carries its objective T2 evidence, not just model vibes.
- ✅ No-LLM mode is a real product, not a broken one.
- ⚠️ Cascades can drop what a lower tier never promoted (e.g., a person who never triggered
  motion thresholds) — mitigated by vendor push events as auxiliary wake signals and by the
  public evaluation set measuring exactly this class of miss.
- ⚠️ More moving parts than a single model — the protocols in `pipeline/base.py` keep tiers
  independently replaceable and testable.
- Tripwire: if edge NPUs make continuous VLM inference cheap within the budget, T1–T3 collapse
  into fewer tiers — the policy layer (T4) and event model survive that unchanged.

## Alternatives considered

- **VLM-on-everything** — violates the compute budget; also latency-hostile.
- **Detector + rules only** — cannot express "looks interested in entering"; this is the
  ceiling we're building past.
- **End-to-end action-recognition model** — monolithic, data-hungry, unexplainable, and
  unfixable per-user; the cascade lets feedback tune thin, inspectable layers.
