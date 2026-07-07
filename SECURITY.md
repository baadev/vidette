# Security policy

Vidette handles the most sensitive data in a home: live video of the people in it. Security
reports are treated as the highest-priority class of issue, ahead of features.

## Reporting a vulnerability

**Do not open a public issue for security problems.**

- Preferred: GitHub → *Security* → *Report a vulnerability* (private advisory), once the
  repository is public.
- Or email **alex@baadev.com** with the subject prefix `[SECURITY]`. If you need encryption,
  say so in a first plain message and we'll arrange a key.

Please include: affected component/version (or commit), reproduction steps or PoC, impact
assessment, and any suggested fix. Reports in English or Russian are both fine.

## What to expect

| Step | Target |
|---|---|
| Acknowledgement | within 72 hours |
| Triage & severity assessment | within 7 days |
| Fix or mitigation for confirmed critical issues | as fast as humanly possible; status updates at least weekly |
| Coordinated disclosure | mutually agreed, default 90 days from report |

Credit is offered in release notes and a `SECURITY-HALL-OF-FAME.md` (opt-in; anonymous if you
prefer). There is no bug bounty yet — this is a young open-source project — but reports are
never met with silence or lawyers.

## Supported versions

Pre-1.0: only the latest release and `main` receive fixes. From 1.0: the latest minor, plus
the previous minor for critical issues.

## Scope

**In scope:** everything in this repository — the server, web app, API, adapters, deploy
artifacts (Dockerfile/compose), and the documented security properties (auth, webhook signing,
"no telemetry", "footage never leaves without config").

**Out of scope, but we still want to know:** vulnerabilities in upstream projects we integrate
(go2rtc, FFmpeg, community bridge sidecars, camera firmware). Report those upstream first; tell us too
so we can pin, mitigate or document.

## Design-level security properties

These are the promises the codebase enforces (details:
[docs/architecture/security-model.md](docs/architecture/security-model.md)):

- No default credentials — first run forces admin creation; `auth: none` requires an explicit,
  loudly-warned config choice.
- Webhooks are HMAC-signed (`X-Vidette-Signature`); API tokens are scoped and revocable.
- Secrets are never logged and never required inside YAML (env interpolation `${VAR}`).
- Zero telemetry by default; no phone-home code paths exist.
- Containers run as non-root; images minimal; dependencies pinned by lockfiles.

If you find behavior contradicting any of these, that's a vulnerability by definition —
report it.
