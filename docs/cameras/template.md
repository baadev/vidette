# <Vendor> — adapter documentation template

> Copy this file to `docs/cameras/<vendor>.md`. Keep the section order — users compare
> vendor pages side by side. Delete instructional comments. State maturity honestly using the
> repo-wide legend (✅ 🚧 📐 🔭).

**Status banner:** adapter tier (A/B/C/D per [README](README.md#support-tiers)), milestone,
affiliation disclaimer if the vendor is trademarked.

## Overview

Two paragraphs max: what this ecosystem is, why people own it, what the pain is, which path(s)
Vidette offers (native protocol vs. sidecar bridge — link the
[bridge pattern](../architecture/plugins.md#the-sidecar-bridge-pattern) if used).

## Paths

For each connection path: prerequisites, exact steps, a config example that validates against
the schema, and a capabilities table:

| Capability | Supported | Notes |
|---|---|---|
| Live main/sub | | |
| Vendor push events | | used as Tier 0 wake signals? |
| Clip download from vendor storage | | |
| Snapshot / PTZ / two-way audio | | |

## Model notes

A table of model families: which support which path, verified-by whom, date. Prefer
community-verified entries via the
[camera support template](https://github.com/baadev/vidette/issues/new?template=camera_support.yml)
over vendor marketing claims.

## Limitations & risks

The honest section: battery/wake behavior, cloud dependencies, rate limits, upstream
reverse-engineering fragility, account/2FA quirks. If the path can break when the vendor
pivots, say so plainly — this page is where users decide what hardware to buy next.

## Credits

Upstream projects this adapter stands on, with links and a sponsorship nudge where deserved.
