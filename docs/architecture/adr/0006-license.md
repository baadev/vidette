# ADR-0006: Apache-2.0 + DCO; no AGPL dependencies in the default install

- **Status:** accepted
- **Date:** 2026-07-07

## Context

Goals: maximum adoption and integration surface (HA ecosystem, bridges, commercial homelab
distros), patent safety around AI components, contributor friction near zero, and future
sustainability options that don't require relicensing. Self-hosted NVRs have low SaaS-clone
risk (nobody can host *your* cameras without your hardware), which weakens the usual argument
for copyleft here. Separately: popular detection models (e.g. the ultralytics YOLO line) are
AGPL — inheriting that would poison downstream integrators.

## Decision

- Code license: **Apache-2.0** (explicit patent grant included), single license for the repo.
- Contributions: **DCO sign-off** (`git commit -s`), no CLA.
- Dependency policy: **no AGPL components or models in the default install**; optional
  user-installed extras must be clearly labeled with their license implications.
- Sustainability, if it comes, is open-core-adjacent services (model packs, hosted relay,
  support) — never relicensing the core (a DCO-based project can't quietly relicense anyway;
  that's a feature: the promise is structural).

## Consequences

- ✅ Frictionless adoption/embedding; patent grant matters for AI-adjacent code; DCO keeps
  drive-by contributions cheap.
- ⚠️ Forks/commercial repackaging are permitted — accepted; the moat is execution and
  community, not license walls.
- ⚠️ Excluding AGPL models costs us some benchmark-leader detectors — accepted; the
  permissive detector field (RT-DETR family, D-FINE, RF-DETR, YOLOX) is strong.

## Alternatives considered

- **MIT** — fine, but no patent grant.
- **AGPL-3.0** — max protection, but deters exactly the integrations and distribution
  channels this product grows through; solves a clone problem we mostly don't have.
- **BUSL/fair-source** — poisons open-source positioning day one; not worth it pre-revenue.
