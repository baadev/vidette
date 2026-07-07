# ADR-0005: Plugins — Python entry points + sidecar bridges

- **Status:** accepted
- **Date:** 2026-07-07

## Context

The product thesis says vendor pain must be an adapter, not a fork. Two realities constrain
the mechanism: (1) the best ecosystem clients are community reverse-engineering projects in
assorted languages (Eufy's is TypeScript) with their own release lives; (2) plugins in a
security product are a supply-chain surface.

## Decision

Two-layer model ([plugins.md](../plugins.md)):

1. **In-process plugins** are Python packages exposing typed protocols via entry points
   (`vidette.adapters`, detector backends, notifiers). Installed by the operator like any
   dependency; no runtime code download; no marketplace service.
2. **Sidecar bridges** wrap non-Python ecosystem clients as separate containers (pinned
   versions) speaking their native APIs (WebSocket/HTTP/MQTT); the in-process adapter stays a
   thin, typed client. First user: `eufy-security-ws`.

> **Update 2026-07-07:** the planned first user never shipped — Anker's backend migration
> killed the legacy API under `eufy-security-ws` before the adapter was built; Eufy is
> served by its native NAS (RTSP) feature through the plain `rtsp` adapter instead
> ([details](../../cameras/eufy.md#why-there-is-no-bridge)). The **decision stands
> unchanged**: the fault isolation it prescribes is precisely what limited this breakage to
> a docs page. Candidate first users are now `ring-mqtt` / `docker-wyze-bridge`, demand-gated.

Capability flags are contractual: the UI renders what adapters claim, and (M5) a conformance
suite verifies claims against behavior.

## Consequences

- ✅ Third-party adapters without forking; upstream reverse-engineering work stays upstream,
  credited, and independently updatable when vendors break things.
- ✅ Crash/fault isolation per ecosystem; core untouched by vendor API churn.
- ⚠️ Sidecars add containers — compose profiles keep the default stack minimal.
- ⚠️ In-process plugins run with full process privileges — mitigated by operator-install-only
  semantics, the supply-chain rules, and (future) an out-of-process adapter host if the
  ecosystem grows enough to warrant it (that would be a new ADR).

## Alternatives considered

- **Port everything to Python** — permanently behind upstream, burns maintainer time on
  protocol archaeology instead of the product.
- **WASM/gRPC plugin sandbox** — attractive isolation, prohibitive friction for the
  contributors we want (CV/int hobbyists); revisit post-1.0 if plugin volume demands it.
- **No plugin system (all adapters in-tree)** — bottlenecks every ecosystem on the
  maintainer; contradicts the thesis.
