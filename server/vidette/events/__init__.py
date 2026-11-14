"""Events: what Vidette tells you about.

Since v0.2 (ADR-0009) events are not produced by an in-house detection cascade. They are
incidents from the upstream NVR (Frigate review items), persisted in the event store and,
from week 2 of the v0.2 plan, judged against your policies. The v0.1 Tier-2 engine and
clip remuxer are frozen at tag `nvr-core`.
"""
