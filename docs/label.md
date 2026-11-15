# Labelling clips: drafts by a model, review on a phone

Hand-labelling 50 clips is the step nobody gets to. `vidette label` lets a VLM draft
every label, lays the drafts out on contact sheets you can check on a phone, and applies
your corrections from a one-line reply such as `3 courier, 7 alert, rest ok`.

Drafts are never ground truth on their own: `vidette eval` does not score them until you
have reviewed them, and the report says how many truth labels are model drafts you
accepted unchanged.

Status: ✅ drafting, sheets and replies, tested with a fake OpenAI-compatible model. Not
yet run with a real model.

## The loop

```bash
cd ~/development/vidette/server

# 1. Draft every clip under clips/ that has no entry yet (default backend: see vlm.md)
uv run vidette label draft ~/vidette-data/front-door
#   drafting with vlm-local: qwen2.5vl:7b at localhost:11434 (/Users/you/.config/vidette/vidette.yaml)
#     Frames stay on this machine (localhost:11434).
#     #1 20261114-210304: courier_at_door (silent, 82%) A man leaves a box by the door.
#     #2 20261114-213011: door_handle_attempt (ALERT, 64%) A person pulls the handle.
#     …
#   added 9 entries (#1-#9) to labels.yaml: 9 drafted, 0 without a draft
#   9 entries to review on 2 contact sheet(s):
#     /Users/you/vidette-data/front-door/review/sheet-0001-0008.png
#     /Users/you/vidette-data/front-door/review/sheet-0009-0009.png

# 2. Look at the sheets, then answer by number
uv run vidette label apply ~/vidette-data/front-door "3 courier, 7 alert, rest ok"

# 3. Measure
uv run vidette eval ~/vidette-data/front-door --judge always --judge vlm-local
```

Run step 1 whenever the collector has added clips: it only touches clips without an
entry, gives them the next numbers and draws sheets for them alone. `vidette label
sheets DIR` redraws the sheets for everything still waiting for review.

## What a draft looks like

The first run creates `labels.yaml` with the starter vocabulary (passer-by, courier,
visitor, door handle, lingering, household member, cat, dog, animal, passing car,
headlights, night noise) and one policy, `entry-interest`; edit both to fit your place
before drafting. Each clip becomes an entry like this:

```yaml
  - id: 20261114-210304
    scenario: courier_at_door        # the model's proposal, not truth yet
    status: draft                    # → reviewed after your reply
    number: 12                       # the big number on the contact sheet
    camera: front_door
    at: 2026-11-14T21:03:04+02:00
    duration_s: 34.2
    media: [clips/20261114-210304.mp4, frames/20261114-210304/1.jpg, …]
    draft:
      by: "vlm-local qwen2.5vl:7b"
      at: 2026-11-14T22:10:00+02:00
      scenario: courier_at_door
      alert: false                   # under the first policy, for your eye only
      description: "A man in a uniform leaves a box by the door and walks back to a van."
      confidence: 0.82
```

The labeller asks the model which of the set's scenarios happened (with their meanings),
not whether to alert: the draft is a proposed fact, and the policy's `alert_on` turns
facts into alert or silent exactly as it does for hand labels. If no scenario fits, the
model may say `unclear`; such entries stay `unlabelled` until you name them.

Everything you wrote by hand in `labels.yaml` (comments, your own entries, the order)
is left as it is: the tool appends entries and rewrites only the blocks it numbered, and
refuses any edit that would make the file invalid (the previous version is kept as
`labels.yaml.bak`).

## Faces

With [known people](people.md) on (`people.enabled`, or `label draft --people`), each new
clip goes through the local face check before it is drafted: the labeller hears who was
recognized, the report lands in the clip's sidecar, and the cell's last line lists the
faces (`faces: 1 Sam .71  2 ? .28`). Answer who a face is in the same reply: `5 Sam`,
`5.2 Robin` when the clip shows several faces, `5 stranger` (`чужой`), `5.2 ?` for "not
sure". `5 ?` alone still leaves the scenario unlabelled.

## Contact sheets

`review/sheet-0001-0008.png`: 8 clips per sheet (`--per-sheet 8` to `12`), two columns,
1080 px wide. Each cell has the review number in a large badge (red: the draft would
alert; grey: silent; amber: unclear or no draft), the proposed scenario and confidence,
four key frames in time order, the time, length and camera, and the model's one-line
description. Send the PNG to a phone **as a file**, not as a photo: messengers shrink
photos to about 1280 px, which turns the description text into mush.

## The reply

| You write | What happens |
|---|---|
| `3 ok` (`ок`, `верно`, `да`, `+`) | the draft is accepted |
| `5 courier` (`курьер`, `courier_at_door`) | the scenario is set; ids, `door handle` with spaces and aliases all work |
| `7 alert` (`тревога`) | accepted if the draft already alerts; otherwise set to the only alerting scenario, or refused with the candidates if there are several |
| `8 silent` (`тихо`) | the same for silent scenarios |
| `9 ?` (`не знаю`, `skip`) | left unlabelled, marked reviewed, not scored |
| `rest ok` (`остальные ок`, `все ок`) | every other numbered draft with a proposal is accepted |

Commas separate items, but `3 курьер 7 тревога` works too. Built-in aliases cover the
starter vocabulary in English and Russian (`кот`, `фары`, `прохожий`, `ручка`, …); add
your own under `aliases:` in `labels.yaml`. Nothing is written unless the whole reply
makes sense: one typo returns the list of problems and changes nothing. `--dry-run`
shows what would change.

You can correct an entry again later by its number; reviewed entries keep the model's
draft, so the eval report can say how many truth labels are drafts accepted unchanged.
That number is the honest measure of how much the truth leans on the model being judged.

## From Telegram

The buttons under a [Telegram alert](telegram.md#the-buttons) are replies too: 👍 sets the
scenario the judge named, 👎 sets `other`, "Не знаю" is `?`, "Это свой" is
`household_member` and a face answer. They go through the same code as `vidette label
apply`; a clip without an entry gets one (with the live verdict as its draft), and the
policy's scenarios are added to the vocabulary if they are missing.

## Without a model

`--no-vlm`, or a model that cannot be reached (Ollama not running, budget spent, bad
key), still gives every clip a number and a place on a sheet, marked "no draft: name it".
Labelling by number works the same way: `1 cat, 2 passer, 3 courier`.

## Observed hours

If the folder was filled by `vidette collect`, each `label draft` refreshes
`observed_hours` from the collector's watched time (the line ends in
`# vidette-managed`). A value you set by hand, without that marker, is never changed.
