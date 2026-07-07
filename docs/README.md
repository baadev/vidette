# Vidette documentation

Status marks used everywhere: ✅ shipped · 🚧 in progress · 📐 designed · 🔭 exploring —
defined in [ROADMAP.md](../ROADMAP.md). A page describing designed functionality says so at
the top; absence of a banner means it describes reality.

## Start here

| If you want to… | Read |
|---|---|
| Run it | [getting-started.md](getting-started.md) |
| Understand the system | [architecture/overview.md](architecture/overview.md) |
| Understand the AI | [architecture/ai-pipeline.md](architecture/ai-pipeline.md) |
| Connect a camera | [cameras/README.md](cameras/README.md) · [Eufy](cameras/eufy.md) · [RTSP/ONVIF](cameras/onvif-rtsp.md) |
| Configure everything | [configuration.md](configuration.md) + annotated [config.example.yaml](../deploy/config.example.yaml) |
| Automate on events | [events-and-automations.md](events-and-automations.md) |
| Use the API | [api.md](api.md) |
| Size hardware | [hardware.md](hardware.md) |
| Judge our choices | [architecture/adr/](architecture/adr/) |
| Harden a deployment | [architecture/security-model.md](architecture/security-model.md) |
| Write an adapter/plugin | [architecture/plugins.md](architecture/plugins.md) |
| Know what the project stands for | [project/principles.md](project/principles.md) |
| Common questions | [faq.md](faq.md) |

## Documentation rules

- English; sentence-case headings; mermaid for diagrams; one idea per page.
- Every page about future functionality carries a status banner with its milestone.
- Numbers are either **measurements** (with hardware + method) or **design targets** (labeled).
  Never a vibe.
- If a page confuses you, that's a documentation bug — [file it](https://github.com/baadev/vidette/issues).
