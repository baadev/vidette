"""Deep analysis (ADR-0019): a slower second look at the day's clips by the big model.

The live judge answers in minutes about four frames, because someone may be woken. Deep
analysis has the night: it looks at more frames of each clip with people or vehicles,
describes what happens step by step, then decides under the same policy from that
description. Its verdict is kept next to the live one (`events.deep`), never over it;
where the two disagree, or either is unsure, the clip goes to the next labelling batch,
and the evening digest quotes its descriptions.

- `analyst`: the two-stage prompt (look, then decide) on one case;
- `compare`: live against deep, in plain categories;
- `worker`: the persistent queue, the night window, the daily cap and the one shared
  slot (vidette.vlm.gate), inside `vidette serve`;
- `cli`: `vidette deep status | show | queue | run`.
"""
