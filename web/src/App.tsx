import { useEffect, useState } from "react";

type Health = { status: string; version: string };

type SystemInfo = {
  milestone: string;
  designed_routes: { path: string; milestone: string }[];
};

const TIERS = [
  { name: "T0 · Motion gate", detail: "~free, always on", milestone: "M2" },
  { name: "T1 · Detection", detail: "tiny model, on motion", milestone: "M2" },
  { name: "T2 · Trajectory geometry", detail: "pure math: approach · dwell · touch", milestone: "M2" },
  { name: "T3 · Scene reasoning", detail: "VLM, rare & budgeted", milestone: "M3" },
  { name: "T4 · Your policy", detail: "plain language, compiled & inspectable", milestone: "M4" },
];

export function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [system, setSystem] = useState<SystemInfo | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/healthz")
      .then((r) => r.json() as Promise<Health>)
      .then(setHealth)
      .catch(() => setError("API unreachable — is the vidette container running?"));
    fetch("/api/v1/system")
      .then((r) => r.json() as Promise<SystemInfo>)
      .then(setSystem)
      .catch(() => undefined);
  }, []);

  return (
    <main className="shell">
      <header>
        <h1 className="wordmark">
          VIDE<span className="accent">TT</span>E
        </h1>
        <p className="tagline">Self-hosted video security that understands intent — not just motion.</p>
      </header>

      <section className="card status">
        <h2>System</h2>
        {error && <p className="error">{error}</p>}
        {health && (
          <p>
            <span className="dot ok" /> API <strong>{health.status}</strong> · v{health.version} ·
            milestone <strong>{system?.milestone ?? "M0"}</strong> — design preview
          </p>
        )}
        {!health && !error && <p>Contacting API…</p>}
        <p className="muted">
          Live wall, timeline and events land in M1–M2. What works today: this shell, the API
          skeleton and <code>vidette validate</code> for your config.
        </p>
      </section>

      <section className="card">
        <h2>The cascade</h2>
        <ol className="cascade">
          {TIERS.map((tier) => (
            <li key={tier.name}>
              <span className="tier-name">{tier.name}</span>
              <span className="tier-detail">{tier.detail}</span>
              <span className="badge">{tier.milestone}</span>
            </li>
          ))}
        </ol>
      </section>

      <footer>
        <a href="https://github.com/baadev/vidette">GitHub</a>
        <a href="https://github.com/baadev/vidette/blob/main/ROADMAP.md">Roadmap</a>
        <a href="/api/docs">API docs</a>
        <a href="mailto:alex@baadev.com">Contact</a>
      </footer>
    </main>
  );
}
