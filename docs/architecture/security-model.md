# Security model

Vidette guards video of your home; its own security posture must exceed what it protects.
This page is the threat model and the hardening guide. Reporting: [SECURITY.md](../../SECURITY.md).

## Assets

1. **Footage & events** — the most privacy-sensitive data in a household.
2. **Camera credentials & vendor accounts** (adapters hold these).
3. **Control plane** — whoever controls Vidette can blind it.
4. **Notification channels** — forged events erode trust; suppressed events hide crime.

## Adversaries considered

| Adversary | Vector | Primary mitigations |
|---|---|---|
| Opportunistic LAN attacker / IoT botnet | open ports, default creds, unpatched deps | no default creds; auth mandatory; minimal surface; pinned deps; non-root container |
| Internet scanner (user exposed the port) | brute force, CVEs | rate-limited login, session hardening, "never port-forward" guidance, VPN-first docs |
| Snooping vendor cloud | camera phones home | cameras on egress-blocked VLAN; Vidette is their only client; local-first design |
| Physical burglar | steals the recorder box; jams Wi-Fi | off-site event backup (M3); wired-camera guidance; instant notifications carry evidence off-box at event time |
| Malicious webhook receiver / MITM | spoofed or replayed events | HMAC signature + timestamp; HTTPS-only targets by default |
| Nosy housemate / insider | shared UI access | per-user accounts, roles (admin/viewer), audit log (M2+) |

## Design rules (enforced, not aspirational)

1. **No default credentials.** First run forces admin creation before any surface is served.
   `auth: none` exists for kiosk LANs but requires an explicit config line and produces a
   permanent UI banner + startup warning.
2. **Secrets never live in YAML.** Config references environment variables (`${VAR}`);
   camera/vendor credentials at runtime live in the DB, encrypted at rest with a key derived
   from a host-provided secret. Secrets are redacted from logs and API responses by type,
   not by discipline.
3. **Signed outputs.** Webhooks carry `X-Vidette-Signature` (HMAC-SHA256 over body +
   timestamp header; receivers should reject > 5 min skew — verification snippets in
   [events-and-automations.md](../events-and-automations.md#verifying-signatures)).
4. **Scoped tokens.** API tokens are per-purpose (`read:events`, `read:streams`, `admin`),
   revocable, and displayed once.
5. **Least surface.** go2rtc admin API is never published; UI/API is one port; metrics
   endpoint can be bound to localhost.
6. **Non-root, minimal, pinned.** Container runs as an unprivileged user; base images slim;
   lockfiles committed; images published by digest (release process, M2+).
7. **No phone-home.** There is no code path that contacts the internet except: destinations
   you configured (webhooks, cloud VLM, off-site backup) and image pulls you initiate. This is
   a testable property — [SECURITY.md](../../SECURITY.md) invites reports if you find otherwise.

## Deployment hardening checklist

- [ ] Cameras on an isolated VLAN/subnet with **no WAN egress**; Vidette is their only client.
      (Cloud cameras that require egress: allow only the vendor endpoints the adapter documents.)
- [ ] Vidette UI reachable via Tailscale/WireGuard, or LAN-only. Never raw port-forward.
- [ ] TLS via reverse proxy (Caddy/Traefik) if exposed beyond localhost.
- [ ] Unique camera passwords (yes, even on the "isolated" VLAN).
- [ ] Media volume on a filesystem with barriers on (defaults are fine); UPS if you can.
- [ ] Off-site event backup once M3 lands — the burglar-takes-the-box scenario.
- [ ] Subscribe to release notifications; security fixes are flagged loudly in release notes.

## Data protection stances

- **Local-first**: footage never leaves the box except to destinations you configure.
- **Cloud VLM opt-in** is per camera, visually marked in the UI, and sends only selected
  keyframes — never continuous streams ([ai-pipeline.md](ai-pipeline.md#tier-3--scene-reasoning-vlm)).
- **Identity is opt-in, local and bounded**: the only biometric feature is
  [trusted faces](../faq.md#what-about-face-recognition) — embeddings of consenting household
  members, computed and stored locally, encrypted at rest, deletable in one click, used
  solely to *suppress* alerts. Uncertain matches never suppress (fail toward alerting); no
  cloud biometrics or third-party identity databases, so the worst-case leak surface stays
  bounded and documented.
- **Export redaction** (blur regions on shared clips) is a designed M3+ feature: sharing
  evidence with neighbors/police shouldn't leak your kids' faces.
- Jurisdiction note in docs: pointing cameras at public space carries legal duties that vary
  by country; Vidette's `public` zone semantics help minimize what you *analyze*, but
  compliance is the operator's responsibility.

## Supply chain

- Python and npm lockfiles committed; CI installs from lockfiles only.
- Dependency review on update PRs; heavy/native deps need an ADR.
- Models are downloaded at setup from pinned URLs with checksums (M2), never at runtime
  silently. SBOM + signed releases: 🔭 M5.

## Audit trail *(M2+)*

Admin actions (config changes, user management, token issuance, footage export/deletion) are
logged to an append-only audit table, visible in the UI. "Who exported that clip" is a
question the system must answer.
