# Architecture Decision Records

Significant, hard-to-reverse choices are recorded here — context, decision, consequences —
so future contributors inherit the *why*, not just the *what*.

Rules: ADRs are immutable once **accepted**; changing course means a new ADR that supersedes
the old one (both stay). New ADRs land as PRs with a comment window. Template:
[template.md](template.md).

| # | Decision | Status |
|---|---|---|
| [0001](0001-runtime-and-languages.md) | Python core + go2rtc sidecar + TypeScript web (hybrid runtime) | accepted |
| [0002](0002-stream-gateway-go2rtc.md) | go2rtc as the stream gateway — integrate, don't reimplement | superseded by 0009 |
| [0003](0003-cascaded-inference.md) | Cascaded inference (T0–T4) as the core AI architecture | accepted, amended by 0009 (T0-T2 delegated to Frigate) |
| [0004](0004-storage-format.md) | Codec-copy fMP4 segments + SQLite index | superseded by 0009 |
| [0005](0005-plugin-model.md) | Plugins: Python entry points + sidecar bridges | superseded by 0009 for camera adapters |
| [0006](0006-license.md) | Apache-2.0 + DCO; no AGPL dependencies in default install | accepted |
| [0007](0007-web-stack.md) | React + Vite + TypeScript; WebRTC/MSE live via go2rtc | accepted |
| [0008](0008-database.md) | SQLite (WAL) + sqlite-vec + FTS5; Postgres deferred | accepted |
| [0009](0009-intent-layer-over-frigate.md) | Intent layer over Frigate; NVR core frozen at tag `nvr-core` | accepted |
| [0010](0010-clip-folders.md) | Clip folders as an incident source; `vidette collect` for wake-on-motion cameras | accepted, amends 0009; amended by 0011 and 0013 |
| [0011](0011-continuous-collect.md) | `vidette collect --continuous`: a bounded ring and a motion gate, still a dataset tool | accepted, amends 0010 |
| [0012](0012-known-people.md) | Known people: local face recognition (YuNet + SFace) that informs the judge | accepted, amends the roadmap's face-recognition scope; amended by 0013 and 0014 |
| [0013](0013-telegram-bot.md) | A Telegram bot with feedback buttons; policy levels decide who wakes | accepted, amends 0010 and 0012 |
| [0014](0014-anonymous-identities.md) | Anonymous identities: unknown faces that come back, counted for the judge | accepted, amends 0012 and principle 4 |
| [0015](0015-truthful-timing-and-early-verdicts.md) | The camera's clock for clips, frames by packet, an early verdict per recording | accepted, amends 0010 and 0013 |
