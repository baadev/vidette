# Vidette documentation

Status marks used everywhere: ✅ shipped · 🚧 in progress · 📐 designed · 🔭 exploring —
defined in [ROADMAP.md](../ROADMAP.md). A page describing designed functionality says so at
the top; absence of a banner means it describes reality.

## Start here

| If you want to… | Read |
|---|---|
| Run it | [getting-started.md](getting-started.md) |
| Connect it to Frigate | [frigate.md](frigate.md) |
| Measure a policy | [eval.md](eval.md) |
| Tell the judge who it saw (opt-in face recognition) | [people.md](people.md) |
| Get alerts in Telegram, with buttons that teach it | [telegram.md](telegram.md) |
| Let 👍/👎 quiet what keeps waking you | [calibration.md](calibration.md) |
| See what every camera recorded at that moment | [other-cameras.md](other-cameras.md) |
| Ask the bot in plain words, or connect an assistant over MCP | [assistant.md](assistant.md) |
| Have the big model look again at the day's clips, overnight | [deep.md](deep.md) |
| Understand the system | [architecture/overview.md](architecture/overview.md) |
| Know why it changed course | [ADR-0009](architecture/adr/0009-intent-layer-over-frigate.md) |
| Configure everything | [configuration.md](configuration.md) + annotated [config.example.yaml](../deploy/config.example.yaml) |
| Automate on events | [events-and-automations.md](events-and-automations.md) |
| Use the API | [api.md](api.md) |
| Judge our choices | [architecture/adr/](architecture/adr/) |
| Harden a deployment | [architecture/security-model.md](architecture/security-model.md) |
| Know what the project stands for | [project/principles.md](project/principles.md) |
| Common questions | [faq.md](faq.md) |

Frozen with the v0.1 NVR core (tag `nvr-core`), kept as records:
[ai-pipeline.md](architecture/ai-pipeline.md) (tiers 0 to 2),
[storage.md](architecture/storage.md), [plugins.md](architecture/plugins.md),
[cameras/](cameras/README.md) (including the [Eufy field notes](cameras/eufy.md)),
[hardware.md](hardware.md).

## Documentation rules

- English; sentence-case headings; mermaid for diagrams; one idea per page.
- Every page about future functionality carries a status banner with its milestone.
- Numbers are either **measurements** (with hardware + method) or **design targets** (labeled).
  Never a vibe.
- If a page confuses you, that's a documentation bug — [file it](https://github.com/baadev/vidette/issues).
