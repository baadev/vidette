# ADR-0007: Web — React + Vite + TypeScript; WebRTC/MSE live via go2rtc; PWA over native apps

- **Status:** accepted
- **Date:** 2026-07-07

## Context

The founding pain is substantially *UI pain*: slow app start, slow live view, single-camera
tunnel vision, baffling controls. The UI must feel instant (budgets: cold load < 2 s on a
phone, live wall sub-second via WebRTC), be hackable by the largest possible contributor
pool, and reach phones without app-store gatekeeping on day one.

## Decision

- **React + TypeScript (strict) + Vite**; dependency-light (no heavy UI framework until a
  real need); dark-first design tokens (night-navy background, single signal-amber accent).
- **Live video:** WebRTC from go2rtc (sub-second), MSE fallback, static-image fallback last.
- **Mobile = PWA first**: installable, web-push (VAPID), offline event review (M5). Native
  wrappers are a 🔭 M5+ question, answered by PWA-limitation evidence, not by default.
- UI consumes only the public API — no private routes ([api.md](../../api.md)).

## Consequences

- ✅ Largest web contributor pool; instant dev loop; PWA ships everywhere without stores.
- ✅ go2rtc does codec negotiation — the web app never touches media plumbing.
- ⚠️ iOS PWA push requires home-screen install; documented honestly (FAQ) rather than papered
  over. Tripwire for revisiting native: a PWA limitation that materially breaks the alert
  loop on either major platform.
- ⚠️ React churn risk — mitigated by staying close to platform primitives and keeping the
  dependency graph shallow.

## Alternatives considered

- **Svelte/SolidJS** — leaner runtimes, smaller contributor pool; the perf bottleneck here is
  video transport, not the framework.
- **HTMX/server-rendered** — poor fit for a live wall + timeline scrubber (client-heavy by
  nature).
- **Native apps day one** — two more codebases before the web one is excellent; the vendor
  apps prove native ≠ good.
