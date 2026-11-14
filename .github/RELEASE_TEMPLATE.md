## Run it

```bash
curl -fsSLO https://raw.githubusercontent.com/baadev/vidette/main/deploy/docker-compose.yml
docker compose up -d
# http://localhost:8642 → create your admin account; setup for this version: README
```

Already running? `docker compose pull && docker compose up -d`.

Published images: `ghcr.io/baadev/vidette` (`linux/amd64`, `linux/arm64`). A stable
release (`vX.Y.Z`) gets this version, `{major}.{minor}` and, if it is the newest stable
release, `latest`; a pre-release (`-rc.N`, `-beta`) gets only its exact version tag and
never moves `latest`.

Release-specific highlights are in [CHANGELOG.md](https://github.com/baadev/vidette/blob/main/CHANGELOG.md);
the commit list below is auto-generated. Current capabilities and honest limits:
[README](https://github.com/baadev/vidette#feature-status) · [ROADMAP](https://github.com/baadev/vidette/blob/main/ROADMAP.md).
Feedback: issues or **alex@baadev.com**.

---
