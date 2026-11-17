# Calibration: 👍/👎 change who is woken

A 👎 under an alert ([telegram.md](telegram.md#the-buttons)) or on the web page's event
card says "this should not have woken me". Calibration counts those marks per scenario
and camera and, when they are clear enough, makes that scenario on that camera quieter:
first it wakes only when the judge is sure, then it goes to the evening digest only. A 👍
on something calibration kept in the digest brings it back at once. Your policy file is
never changed; every step is stored, said in one line and can be taken back with one tap.
Design and guardrails: [ADR-0020](architecture/adr/0020-calibration-from-marks.md).

Status: ✅ tested against a fake Telegram API, a temporary database and the web API;
replayed over a test deployment's real marks with
[`vidette policy dry-run`](#dry-run-what-a-policy-would-have-done)
([measured](#measured-on-a-test-deployment)). Calibration changes routing only; it is not
measured by `vidette eval` (which scores the judge, not who was woken).

## The rule

A *key* is one scenario of one policy on one camera ("person at the house" on the
porch). Its ladder:

| Step | What an alert of it does |
|---|---|
| 0 as written | what the policy says |
| 1 a bar | wakes only when the judge is at least `bar` (90 %) sure; below, the digest |
| 2 the digest | goes to the digest; it is still confirmed and listed there |

**One step quieter** when, since the key's last change and within `window` (14 days):

- at least `marks` (4) 👎 that count, and
- not one 👍.

Every reason under 👎 counts except three that are not the policy's fault: "The
recording froze" and "Wrong camera" (the camera), and "One of us or familiar" (telling the
household apart is the face check's job; a stricter bar would hide strangers too). A 👎
without a reason counts. When most of the marks say "Right, but not important", the bar
is skipped and the key goes straight to the digest: the judge saw right, so its
confidence is not the problem.

Each step needs fresh marks: those that made a step never count for the next one, so the
digest takes at least eight 👎.

**Back:** a 👍 on an event that a step kept out of the alerts (from the web page's event
card) takes that step back at once. Slow to quiet, quick to wake again: a mistake should
cost a few extra alerts, never a missed one.

**Never quieter than this.** A scenario of a level with `wake: always` (the critical
ones: someone at the panel, at a door handle, climbing over) and any scenario or level
named in `calibration.protect` can get at most a bar of `protected_bar` (75 %) and never
goes to the digest. The limit is checked again for every alert, so a step made before a
scenario became protected is capped too. If the policy's own `sensitivity` already asks
that much (`relaxed`: 75 %), a protected scenario cannot get stricter at all.

Other things calibration leaves alone: a verdict the [recurrence rule](people.md#strangers-who-come-back)
raised (it is about who comes back, not what they did), `other`, policies without
levels, quiet hours and known people (those stay as the policy says).

## What you see

In Telegram, silently, in the alerts topic of every recipient, one line with a button:

```
🎚 Person at the house (Porch) now wakes you only when the judge is 90 % sure or
more: 4 times 👎 “walked past”.            [↩️ Undo]
```

With `language: ru`: «🎚 Человек у дома (Крыльцо) теперь только в сводке: 4 раза
👎 «прошёл мимо».» Mixed reasons read "6 times 👎 (walked past 3, no reason 3)".

- **↩️ Undo** takes that step back (and any later step of the same key); the line is
  edited to say how it wakes now.
- **`/tuning`** lists what is in force, numbered, with one button per change.
- The digest marks what calibration kept out: "• 20:05 Porch: person at the house ·
  in the digest by your marks".
- A test alert (`vidette telegram try`) says when calibration would have sent it to the
  digest.

On the web app's **System** page, the Calibration card shows the rule, what is in force
with an Undo button each, and every earlier change with who took it back (`telegram`,
`web`, `cli`, or `mark` for a 👍 on what it hid).

From the command line:

```bash
vidette policy --config ~/.config/vidette/serve.yaml calibration          # every step
vidette policy --config ~/.config/vidette/serve.yaml calibration undo 3   # take one back
```

## Configuration

```yaml
calibration:
  enabled: true          # false: 👍/👎 only become labels, as before
  marks: 4               # 👎 for one step, with no 👍 among them
  window: 14d            # older marks do not count
  bar: 0.9               # the first step: wakes only from this confidence
  protected_bar: 0.75    # the most a protected scenario's bar can be raised to
  protect: []            # more scenarios or levels that never leave the alerts
                         # (every scenario of a `wake: always` level is protected anyway)
```

`vidette validate` refuses a `protect` name that no policy has, and a `protected_bar`
above `bar`.

## Dry run: what a policy would have done

`vidette policy dry-run` replays the stored verdicts of the last days through the current
policy (or one you propose) and calibration, and prints what would have woken someone,
gone to the digest or stayed silent, per day and per scenario and camera. It works for
every event in the database, clip folders and Frigate alike. Nothing is sent, no model is
called, and the database is opened read-only, so it is safe next to a running service.

```bash
vidette policy --config ~/.config/vidette/serve.yaml dry-run --days 7
vidette policy --config ~/.config/vidette/serve.yaml dry-run --calibration off
vidette policy --config ~/.config/vidette/serve.yaml dry-run --policy-file proposed.yaml
```

| Option | |
|---|---|
| `--calibration replay` (default) | no steps at the start, then every 👍/👎 at the time it was given, every step it would have made applied to the events after it: what calibration would have done had it been on |
| `--calibration active` | the steps in force now, as if they had held all along |
| `--calibration off` | the policy exactly as written |
| `--policy-file FILE` | a YAML file with `policies:` (and optionally `calibration:`) used instead of the config's |
| `--verdicts live \| deep \| best` | the live judge's verdicts (default), deep analysis' ([deep.md](deep.md)), or the live ones and deep analysis where the live judge gave none |
| `--days N`, `--db PATH`, `--json`, `--env-file FILE` | |

The output's shape (an illustration, not a measurement):

```
day           events   wake  (written)  digest  silent  unjudged  sent then  marked 👎/👍
2026-01-14       100      3          3       0      60        37          3          2/1
2026-01-15       100      1          2       1      60        38          2          1/0
total            200      4          5       1     120        75          5          3/1

Calibration moved 1 alert(s) to the digest.
Steps:
  01-14 18:20  person_at_house on Porch: wakes only from 90 % sure (4 × 👎: walked past 4)
```

`wake` is with the chosen calibration, `(written)` without it, `sent then` the alerts the
bot really sent (quiet hours, collapsed series and policy edits since then make it
differ), `marked` the 👎/👍 those days' events got. `unjudged`: the event has no stored
verdict (the live queue never reached it, or the judge gave none).

What a replay cannot show: a change in a scenario's *wording*. The judge read the old
wording; only judging the clips again tells what the new one does
([`vidette eval`](eval.md) on a labelled set). Moving a scenario to another level,
changing a `wake` rule, `sensitivity` and calibration are replayed exactly; quiet hours are
not (they are per chat and per moment).

### Measured on a test deployment

Clip-folder cameras, its first four days (the `--days 7` window reaches back to its
start), stored live verdicts. As written, the policy would have woken someone 16 times;
the bot really sent 18 alerts (two were early alerts the whole clip then called ordinary)
and they got 13 👎 and 1 👍. Calibration replayed from those marks makes one step on the
second morning (a 90 % bar for one scenario on one camera, after four 👎 without a
reason) and moves 3 of the 16 to the digest: two the household had marked false and one
nobody answered; none it had marked right, and no critical scenario. About two thirds of
the clips have no live verdict at all (the shared model did not keep up), which no
calibration fixes.

## Budgets

| | |
|---|---|
| Compute | after a mark: one query over the marked events of the window and the steps; per alert: one query over the steps (a few rows) |
| Storage | one row per step in `calibration`, never deleted; `intent.calibration` on events it sent to the digest |
| Network | one short Telegram message per step, to each recipient |
| Latency | none measurable for an alert |
