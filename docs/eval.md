# Measuring a policy: `vidette eval`

Vidette's whole promise is fewer, better alerts. `vidette eval` is how that promise gets a
number: it runs a judge over a set of labelled clips and reports precision, recall, false
alarms per day and what a VLM would cost at that volume. Nothing in this repository claims
an accuracy figure that did not come out of this command on a named dataset
([ADR-0009](architecture/adr/0009-intent-layer-over-frigate.md)).

Status: the harness, the dataset format, four model-free baselines, the VLM judges and
the stored-verdict judges work. First numbers on a test deployment are
[below](#first-numbers-on-a-test-deployment).

## Try it

```bash
cd server
uv run vidette eval ../datasets/synthetic-sample
uv run vidette eval ../datasets/synthetic-sample --judge frigate-severity --json
uv run vidette eval ../datasets/synthetic-sample --usd-per-image 0.001 --frames-per-call 4
```

The sample set is five synthetic entries (flat boxes drawn with ffmpeg). It exists to show
the format and exercise the code; its numbers mean nothing about real footage.

With [known people](people.md) set up, `--people` runs the local face check on every
entry and tells VLM judges who was recognized; `--known-people quiet` tries the "known
people never need an alert" rule without editing the dataset's policies.

## What the report says

```
judge              alerts  TP  FP  FN  abst  precision (95% CI)  recall (95% CI)     false alarms/day
always                  5   1   4   0     0  0.20 (0.04-0.62)    1.00 (0.21-1.00)                 4.0
frigate-severity        2   1   1   0     0  0.50 (0.09-0.91)    1.00 (0.21-1.00)                 1.0
```

- **TP / FP / FN**: alerts that were due, alerts that were not, due alerts that never came.
- **precision**: of the alerts sent, the share that was due. **recall**: of the due
  alerts, the share that was sent.
- **95% CI**: a Wilson score interval. With 30 to 50 clips the interval is wide, and it is
  printed so nobody reads 1.00 on three alerts as a guarantee.
- **abst**: entries the judge had nothing to go on for (for example `frigate-genai` on an
  incident Frigate never summarized). An abstention counts as "no alert", because that is
  what would happen at home.
- **false alarms/day**: false positives divided by `observed_hours / 24`. Without
  `observed_hours` in the labels, per-day numbers are not printed at all.
- **VLM cost**: calls and images the judges actually sent, plus a projection for a VLM
  judge at one call per incident (`--frames-per-call`, default 4) priced with
  `--usd-per-call` and `--usd-per-image`. Prices default to 0, which is right for a local
  model and wrong for a hosted one: look up your provider's price per image at your frame
  size.
- A per-scenario table (`alerted / total`) and the ids of every false alarm and miss.
- **By day**: false alarms and misses per judge for each calendar day the entries cover
  (from their `at`), when they cover more than one.

`--json` prints the same numbers for scripts and dashboards.

## Judges available now

| Judge | Alerts when | Reads the policy? |
|---|---|---|
| `always` | always: "notify on every Frigate review", the false-alarm ceiling | no |
| `never` | never: the recall floor | no |
| `frigate-severity` | Frigate marked the review `alert` (its alert rules, usually "person or car in a zone") | no |
| `frigate-genai` | Frigate's GenAI review summary (0.17+) rates the threat 1 or 2; abstains without a summary | no |
| `vlm-local` | the VLM judge on your local backend (Ollama by default) answers `alert` | yes |
| `vlm-cloud` | the VLM judge on your cloud backend answers `alert` | yes |
| `stored-live` | the live judge of a running Vidette said alert, read from its database: no model call | it did, then |
| `stored-deep` | deep analysis said alert ([deep.md](deep.md)), read from the database | it did, then |
| `stored-best` | the live verdict, or deep analysis' where the live judge gave none | it did, then |

The baselines do not read your policy text; the VLM judges do ([vlm.md](vlm.md)). The
point of the table is to see whether reading it beats the cheapest baseline by enough to
pay for itself. VLM judges run only when named with `--judge`, read their backends from
`--config` (default `$VIDETTE_CONFIG`, then `~/.config/vidette/vidette.yaml`), and add a
table of what each run measured: calls, images, tokens, cost from your prices, latency
p50/p95.

## Scoring what the service already said

A running `vidette serve` keeps every verdict it gave. `stored-*` judges score those
instead of asking a model again, so measuring costs nothing and takes no slot from a
shared model server:

```bash
vidette eval ~/vidette-data/porch ~/vidette-data/garden \
  --config ~/.config/vidette/serve.yaml --policy-from-config home \
  --judge stored-live --judge stored-deep --judge always --judge never
```

- **Several datasets as one:** name them all; entry ids get their set's name as a prefix
  (`porch.20260114-101500`), vocabularies merge, a policy is kept when every set has it
  with the same `alert_on`, and per-day rates use the longest `observed_hours` (the sets
  of one place run side by side).
- **`--policy-from-config NAME`**: the ground truth of a service's policy with levels:
  every scenario of a level that alerts (`wake` other than `never`), as far as the set's
  vocabulary knows it. The stored verdicts are matched by the same policy name.
- **`--db PATH`**: the database to read (default: the config's `storage.database`). It is
  opened read-only; a running service is not disturbed.
- An entry is matched to its event by the clip's path. A stored judge abstains, and the
  report counts why, when the clip is **not in the database** (recorded before the service
  watched the folder) or **not judged** (the live queue never reached it, deep analysis did
  not look at it, or the judge gave no answer). An abstention counts as no alert, which
  is what happened at home.
- The stored `alert` is what the service decided then, under the policy as it was that
  day; a policy edited since is not re-applied.
- Only `status: reviewed` entries (and hand-written ones) are truth; machine drafts are
  not scored unless `--include-drafts` says so, loudly.

## First numbers on a test deployment

Clip-folder cameras on a test deployment over its first four days (November 2026), the
live judge a 27B vision model on a shared llama-server, a policy with three levels;
scored with the stored judges, no model call. `frigate-severity` and `frigate-genai`
abstain on every entry (no Frigate here) and are left out.
The set is every clip a person reviewed: 32 entries, 3 of which should alert under the
policy. It is small and it is skewed: 12 of the 32 are alerts the household answered in
Telegram (so precision here is close to "of the alerts that got an answer, how many were
right"), 20 are clips reviewed on contact sheets that were recorded before the service
ran (every stored judge abstains on those), and recall rests on three clips.

| judge | alerts | TP | FP | FN | abstained | precision (95 % CI) | recall (95 % CI) |
|---|---|---|---|---|---|---|---|
| stored-live | 12 | 2 | 10 | 1 | 20 | 0.17 (0.05 to 0.45) | 0.67 (0.21 to 0.94) |
| stored-deep | 3 | 1 | 2 | 2 | 22 | 0.33 (0.06 to 0.79) | 0.33 (0.06 to 0.79) |
| always | 32 | 3 | 29 | 0 | 0 | 0.09 (0.03 to 0.24) | 1.00 (0.44 to 1.00) |
| never | 0 | 0 | 0 | 3 | 0 | n/a | 0.00 (0.00 to 0.56) |

What the live service did over the same days (from its database, not from the set):
most clips were never judged (the shared model judged about a third of them: newest
first, older than 15 minutes dropped); of 18 alerts sent, the household marked 13 false
and 1 right (precision of the answered alerts 0.07, 95 % CI 0.01 to 0.31), 4 got no
answer. After the policy was reworded to ask for the act on the
property (on the second day), the next 50 hours had 5 alerts: 3 marked false, 1 right,
1 without an answer.

Read these as a measurement of this one setup, not of Vidette: three positives cannot
carry a recall figure.

## Dataset format

A dataset is a directory with a `labels.yaml` and the media it points at:

```
home-front-door/
  labels.yaml
  clips/2026-11-14_2103_porch.mp4
  frames/2026-11-14_2103_porch/001.jpg
```

```yaml
version: 1
name: home-front-door
description: Everything the porch camera recorded, 7-14 Nov 2026
license: private, never publish            # required; say who may see it
observed_hours: 168                        # wall-clock span the entries cover (for per-day)

scenarios:                                 # your vocabulary: what can happen
  passer_by: walks past on the street, never approaches
  courier_at_door: walks up, leaves a parcel, leaves
  door_handle_attempt: tries the door handle, a door or a window
  # …

policies:                                  # ground truth: which scenarios should alert
  entry-interest:
    description: >                         # what a judge is told, in plain language
      Alert me when someone shows interest in getting in. Ignore passers-by, couriers,
      cats and car headlights.
    alert_on: [door_handle_attempt, lingering_at_door]

entries:
  - id: 2026-11-14-2103-porch              # letters, digits, . _ -
    scenario: door_handle_attempt          # one of `scenarios`, or `unlabelled`
    camera: front_door                     # optional, default "camera"
    at: 2026-11-14T21:03:00+02:00          # optional; naive times are read as UTC
    duration_s: 33                         # optional
    media: [clips/2026-11-14_2103_porch.mp4]
    upstream:                              # optional: what Frigate (or the camera) said
      severity: alert                      # alert | detection
      objects: [person]
      zones: [porch]
      sub_labels: []
      threat_level: 1                      # Frigate GenAI potential_threat_level, 0-2
      summary: A person tried the handle twice.
    notes: free text for yourself
    reason: passing                        # optional: why an alert was wrong (the bot's list)
```

Entries written by `vidette label` also carry `number` (on the review sheets), `status`
(`draft` until a person reviews it, then `reviewed`) and the model's `draft`; drafts are
not scored until reviewed ([label.md](label.md)). An answer from the Telegram bot adds the
person's `reason` when they said why an alert was wrong (`passing`, `light`, `stalled`,
`unimportant`: the alert was right but not worth it, …; [telegram.md](telegram.md#the-buttons));
it is not scored, it is what the false-alert analysis counts. An optional top-level `aliases:` maps
extra review words to scenarios.

Rules the loader enforces (each failure names the entry):

- unknown keys are errors, as in Vidette's config;
- every `scenario` must be listed under `scenarios`, except the reserved `unlabelled`,
  which keeps an entry in the set but out of the score;
- every `alert_on` label must be a known scenario;
- entry ids are unique; media paths exist, stay inside the dataset directory and are
  images (`.jpg .jpeg .png .webp`) or videos (`.mp4 .mov .mkv`).

Ground truth lives in the policy (`alert_on`), not in the entry. The same clips can then be
scored against several policies, and an entry says only what happened, which is a fact
rather than an opinion about what should alert.

### A starter vocabulary

Use what fits your place; these are the scenarios the plan names:

| Scenario | What happened |
|---|---|
| `passer_by` | someone walks past on the street or sidewalk and never approaches |
| `courier_at_door` | a delivery: walks up, leaves a parcel, leaves |
| `visitor_at_door` | rings or knocks and waits |
| `door_handle_attempt` | tries the door handle, a door or a window |
| `lingering_at_door` | stays at the door or a window with no visible errand, peers in |
| `household_member` | someone who lives here |
| `cat`, `dog`, `animal` | an animal |
| `car_passing`, `car_lights` | a car drives by, or only its headlights sweep the scene |
| `night_noise` | IR noise, insects, rain or shadows; nothing real |

## Adding your own clips (battery cameras and friends)

A battery camera cannot feed Frigate continuously, but its event clips are exactly the
kind of material a policy has to judge. Until a mains camera runs under Frigate, build the
first real set from them.

**The automated way:** [`vidette collect`](collect.md) records every wake period of the
camera into a dataset folder, and [`vidette label`](label.md) drafts the labels with a
VLM and lets you review them by number from contact sheets. The manual way, with clips
exported from the Eufy app:

1. **Export the clips.** In the Eufy app, open an event and download it; the clip lands in
   the phone's gallery as an MP4 (menu names change between app versions). Move the files
   to your computer, for example with AirDrop.
2. **Keep the set outside this repository**, for example `~/vidette-data/home-front-door/`,
   with the clips under `clips/`. Real footage shows neighbours, couriers and number
   plates: never commit it, never attach it to an issue. Publish numbers, not clips.
3. **Start the labels file** from the sample:
   `cp datasets/synthetic-sample/labels.yaml ~/vidette-data/home-front-door/`, then set
   `name`, `license: private, never publish`, your `scenarios` and `policies`, and delete
   the sample entries so that `entries:` is the last line of the file.
4. **Generate entries for the clips**:

   ```bash
   uv run vidette eval ~/vidette-data/home-front-door --scaffold >> ~/vidette-data/home-front-door/labels.yaml
   ```

   Each clip becomes an `unlabelled` entry. Watch each one and set its `scenario`; fill
   `at` from the file name if you want day/night breakdowns later.
5. **Set `observed_hours`** to the span the clips cover, for example 168 if you exported
   every event of one week. Only events the camera itself recorded are in the set: a
   person the camera's own motion trigger missed is invisible here, so recall is measured
   against what the camera caught, not against everything that happened.
6. **Run it**: `uv run vidette eval ~/vidette-data/home-front-door`.

Without Frigate the `upstream` fields stay empty, so `frigate-severity` and
`frigate-genai` abstain on these entries; `always` gives the "every event notifies"
baseline, which is what the Eufy app does today. Once Frigate runs, `vidette frigate tail
--record` captures real review items to fill `upstream` from.

Aim for 30 to 50 entries with every scenario represented a few times, including the boring
ones (passers-by, headlights): they are where false alarms come from.
