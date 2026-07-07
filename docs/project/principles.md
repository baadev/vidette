# Engineering principles

The constitution. PRs are reviewed against this page; when a principle and a feature
conflict, the feature loses or the principle is amended here, in public, with an ADR.

## 1. The three budgets

**Compute, storage, latency** are product features, not implementation details.

- Every milestone defines numeric budgets on reference hardware ([ROADMAP](../../ROADMAP.md));
  a milestone isn't done until they hold.
- Every release publishes measurements; regressions block the release.
- Every PR that plausibly moves a budget states the impact — measured when possible, reasoned
  when not. "It's probably fine" is not a statement.
- Numbers in docs are either **measurements** (hardware + method attached) or **design
  targets** (labeled). Never adjectives doing arithmetic's job.

## 2. Recording is sacred

The recorder is the one promise that must never break. Analysis, previews, notifications, UI —
everything sheds load before one frame of recording is at risk. A worker crash never touches
the recorder. Storage failures are announced as loudly as intruders: a security system that
silently stops recording is worse than none.

## 3. Local-first, consent-explicit

Data leaves the box only toward destinations the user configured, and the config names them
explicitly. No telemetry by default, forever. No accounts, no phone-home, no silent model
downloads. When cloud services are used (VLM, off-site backup), what exactly is sent is
documented and minimized (keyframes, not streams; clips, not archives).

## 4. Behavior first; identity only by consent

Vidette judges *what is happening*, not *who someone is*. The single identity feature is
**trusted faces**: opt-in, local-only matching of people who consented to enrollment, used
exclusively to *suppress* alerts they cause
([guardrails](../faq.md#what-about-face-recognition)). Uncertain matches never suppress —
the system fails toward alerting. No stranger identification, no third-party identity
databases, no cloud biometrics: a values line and a scope decision at once — the intent
problem is bigger and less served.

## 5. Honest software

- One status legend (✅ 🚧 📐 🔭 ❌) used everywhere; designed ≠ shipped, and the software
  itself says so (`501 designed` API stubs, validator warnings for design-stage config).
- Errors tell the user what to do next; logs name the failing camera, not just the exception.
- No dark patterns, no fake progress bars, no "AI" labels on if-statements.
- The false-alert rate is a first-class published metric — this product's value *is* the
  quality of its silence.

## 6. Boring tech, reused giants

SQLite before Postgres, one container before a cluster, sidecar before rewrite, go2rtc/FFmpeg/
ONNX Runtime/Apprise before NIH. Complexity budget is spent in exactly one place: the
understanding cascade. Upstream fixes go upstream; credit flows where the work happened.

## 7. The user is the judge

AI proposes, the human disposes. Alerts carry evidence (clip, snapshot, reasoning trace);
thresholds are visible and tunable; feedback (👍/👎) changes behavior in inspectable ways
(no invisible drift); policy dry-runs show what *would* fire before it does. No action
automation (sirens, locks) in core until the trust machinery has earned it.

## 8. Onboarding is a feature with a budget too

Zero → first live camera in under 5 minutes; first useful alert in under 15. Every question
the setup asks must earn its place. If a user needed the troubleshooting page, we file it as
a UX bug first and a docs improvement second.

## 9. Quality bar

Typed (mypy strict / TS strict), linted, tested where logic is pure, CI green, docs land with
code, ADRs for irreversible choices, conventional commits, DCO. We are building infrastructure
people point at their homes; "works on my machine" is not a standard. No govnokod — the term
stays untranslated because everyone recognizes it on sight.
