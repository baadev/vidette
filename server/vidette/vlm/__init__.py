"""The VLM client: one OpenAI-compatible chat client for a local and a cloud backend.

`endpoint` turns the config into a concrete endpoint (URL, model, limits, prices, who
receives the frames), `budget` keeps the daily call budget and the per-minute pace, and
`client` makes the call: frames as data URLs, a JSON-schema answer, a timeout, latency
and cost accounting. The judge that uses it lives in `vidette.verdicts.vlm`.
"""
