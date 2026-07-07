# Architecture Decision Records

Significant, hard-to-reverse choices are recorded here — context, decision, consequences —
so future contributors inherit the *why*, not just the *what*.

Rules: ADRs are immutable once **accepted**; changing course means a new ADR that supersedes
the old one (both stay). New ADRs land as PRs with a comment window. Template:
[template.md](template.md).

| # | Decision | Status |
|---|---|---|
| [0001](0001-runtime-and-languages.md) | Python core + go2rtc sidecar + TypeScript web (hybrid runtime) | accepted |
| [0002](0002-stream-gateway-go2rtc.md) | go2rtc as the stream gateway — integrate, don't reimplement | accepted |
| [0003](0003-cascaded-inference.md) | Cascaded inference (T0–T4) as the core AI architecture | accepted |
| [0004](0004-storage-format.md) | Codec-copy fMP4 segments + SQLite index | accepted |
| [0005](0005-plugin-model.md) | Plugins: Python entry points + sidecar bridges | accepted |
| [0006](0006-license.md) | Apache-2.0 + DCO; no AGPL dependencies in default install | accepted |
| [0007](0007-web-stack.md) | React + Vite + TypeScript; WebRTC/MSE live via go2rtc | accepted |
| [0008](0008-database.md) | SQLite (WAL) + sqlite-vec + FTS5; Postgres deferred | accepted |
