# ADR-0013: A Telegram bot with feedback buttons, and policy levels that decide who wakes

- **Status:** accepted
- **Date:** 2026-11-14
- **Amends:** [ADR-0010](0010-clip-folders.md), decision 1 ("Vidette only reads the
  folder"), and [ADR-0012](0012-known-people.md), decision 6 ("Vidette does not override
  an `alert` verdict because a known face was in the clip"), each in one bounded way
  described below. Everything else in those ADRs stands.

## Context

- Vidette now judges clip folders live, and a verdict that stays on the Events page
  protects nobody. People read alerts on a phone; the
  [roadmap](../../../ROADMAP.md) had "Telegram via Apprise with the verdict's reason and
  👍/👎" as designed.
- Apprise can send a message, but not buttons, and it cannot receive anything. Feedback
  is the point: every tap on an alert is a label the evaluation set needs
  ([eval.md](../../eval.md)), and today labelling happens only through contact sheets.
- A household's rules are graded, not binary. A typical set: someone tampering with a meter box, trying a car's doors or climbing
  a fence must wake people even when it is one of the household; a person standing in a
  yard must wake people only if the person is not recognized or the face cannot be seen;
  a passer-by, a car driving past or a cat goes into a summary. One alert/silent policy
  cannot say that.
- A machine running Vidette at home is usually behind NAT with no public URL; a webhook
  would need a tunnel or an open port.

## Decision

1. **A native Telegram bot inside `vidette serve`**, not an Apprise channel:
   `notifications.telegram` with a bot token (`${VAR}` only), a list of allowed chats and
   their forum topics. It uses long polling (`getUpdates`): no webhook, no open port, no
   third-party relay. It answers only allowed chats; button presses and commands from any
   other chat get no answer at all.
2. **Policy levels.** A policy may list levels, highest first, each with its scenarios and
   a `wake` rule: `always`, `unless_quiet`, `unless_known`, `never`. The judge names one
   scenario (or `other`); the scenario's level decides whether the event is confirmed
   (an alert) and who wakes. A policy without levels works as before.
3. **Routing:** an alert goes to the alerts topic at once; `unless_known` levels go to the
   digest instead when the face check recognized everyone the judge counted (at least one
   known face, no unmatched face, and the judge's person count no larger than the known
   ones); quiet hours (`/quiet`) hold everything except `always`. Everything that did not
   wake goes into an hourly digest built from the database. One camera's alerts within a
   few minutes edit the first message instead of buzzing again; an alert is sent once per
   event and chat (a unique index, not a hope).
4. **Answers become labels.** Under each alert: 👍 right, 👎 false alarm, "This is …"
   (a person enrolled in known people), "Not sure". An answer is the event's feedback
   (`up` / `down`) and, when the clip lives in a dataset folder
   (`<dataset>/clips/<id>.mp4`), a **reviewed label in that dataset's `labels.yaml`**,
   through the same code as `vidette label apply`. "This is …" also confirms the face
   (`vidette people confirm`).
5. **Events without a verdict never alert.** When the judge is unreachable, over budget or
   unsure, nothing is sent by a simpler rule; the digest counts such events with the
   reason ("judge unavailable", "no answer in time").

### The two amendments, bounded

- **ADR-0010 (clip folders are read only):** the clip-folder *source* still never writes.
  The bot's answers write one file, the dataset's `labels.yaml` (with `labels.yaml.bak`
  and a temporary `.labels.yaml.new` next to it), and only when `labels: true` (the
  default) and the clip is in a dataset layout. Clips, frames and sidecars are not
  touched. Every write is validated as a dataset before it replaces the file.
- **ADR-0012 (known faces never quiet an alert):** they still never quiet an alert by
  themselves. A level marked `wake: unless_known` is the household's own explicit rule,
  and it routes, it does not silence: the event stays confirmed and appears in the
  digest. The check is the narrow one in decision 3, so a second person whose face never
  turned to the camera, an unclear face or a judge that did not count people all wake
  someone. `always` levels ignore faces entirely.

## Consequences

- ✅ Alerts reach phones with the frames, the clip and what the judge saw; every tap is a
  reviewed label, so the labelled set grows from use instead of from sessions with
  contact sheets.
- ✅ No inbound connection, no public URL; the bot works from behind NAT.
- ⚠️ Telegram is a cloud service. Frames, clips, the judge's description and, with
  `names: true`, the names of recognized people pass through Telegram's servers. This is
  a destination the user configures explicitly, like a cloud judge; it is off unless
  configured, `names` is off by default, and `vidette validate` says so when it is on.
- ⚠️ Clip folders deliver an incident only after the clip ends, and the judge may take
  minutes on a shared model: an alert is late by the clip's length plus the judge's
  latency. The verdict worker now judges the newest waiting incident first, so a backlog
  delays old clips, not the current one; clips older than 15 minutes are not judged.
- ⚠️ Policies with levels are not yet measured by `vidette eval` (its judges still answer
  alert / silent from the description); the labels the bot writes use the levels'
  scenarios, so the data for that measurement accumulates.
- ⚠️ A `labels.yaml` edited by hand at the same moment as a tap can lose one of the two
  edits (the file is replaced atomically, but not locked across processes).
- **Tripwire:** if the bot starts to need a webhook, a server of its own, a user database
  or chats beyond the configured ones, stop: that is a notification service, and the
  answer is Apprise, ntfy or Home Assistant's own app.

## Alternatives considered

- **Apprise (`tgram://`)**: sends, cannot receive; no buttons, so no feedback. Still the
  right answer for every other destination.
- **A webhook bot**: lower latency for button presses, but needs a public HTTPS endpoint
  into the home network.
- **Two policies (critical, elevated) instead of levels**: two VLM calls per incident on
  a model that already queues, and no single scenario to label.
- **Silencing known faces in the judge's prompt** (`known_people: quiet`) for the elevated
  level: leaves the decision to the model's reading of one line; the routing rule is
  deterministic, testable and shown in the digest.
