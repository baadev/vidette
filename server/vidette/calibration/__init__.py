"""Calibration (ADR-0020, docs/calibration.md): 👍/👎 change who is woken.

- `rules`    the ladder (as written → a confidence bar → the digest), what counts, the
             safety floor for protected scenarios; pure
- `service`  `Calibrator` inside `vidette serve`: reviews after every mark, applies
             steps to new alerts, takes them back
- `dryrun`   `vidette policy dry-run`: stored verdicts replayed through a policy and
             calibration, nothing sent
- `cli`      `vidette policy calibration` and `vidette policy dry-run`
"""
