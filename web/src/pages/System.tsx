import { useEffect, useState } from "react";
import { UNAUTHORIZED_EVENT } from "../api";

type Health = { status: string; version: string };

type GatewayInfo = {
  reachable: boolean;
  version: string | null;
  streams: string[];
  detail: string;
};

type SystemInfo = {
  milestone: string;
  config_warnings: string[];
  auth_mode: string;
  gateway: GatewayInfo;
  designed_routes: { path: string; milestone: string }[];
};

const TIERS = [
  { name: "T0 · Motion gate", detail: "~free, always on", milestone: "M2" },
  { name: "T1 · Detection", detail: "tiny model, on motion", milestone: "M2" },
  {
    name: "T2 · Trajectory geometry",
    detail: "pure math: approach · dwell · touch",
    milestone: "M2",
  },
  { name: "T3 · Scene reasoning", detail: "VLM, rare & budgeted", milestone: "M3" },
  { name: "T4 · Your policy", detail: "plain language, compiled & inspectable", milestone: "M4" },
];

async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { accept: "application/json" },
  });
  if (response.status === 401) {
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    throw new Error("session expired");
  }
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return (await response.json()) as T;
}

export function SystemPage() {
  const [health, setHealth] = useState<Health | null>(null);
  const [system, setSystem] = useState<SystemInfo | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchJson<Health>("/healthz")
      .then((data) => {
        if (!cancelled) setHealth(data);
      })
      .catch(() => {
        if (!cancelled) setError("API unreachable — is the vidette container running?");
      });
    fetchJson<SystemInfo>("/api/v1/system")
      .then((data) => {
        if (!cancelled) setSystem(data);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  const gateway = system?.gateway;

  return (
    <div className="page-narrow">
      <section className="card status">
        <h2>System</h2>
        {error && <p className="error">{error}</p>}
        {health && (
          <p>
            <span className="dot ok" /> API <strong>{health.status}</strong> · v{health.version} ·
            milestone <strong>{system?.milestone ?? "M1"}</strong>
          </p>
        )}
        {!health && !error && <p>Contacting API…</p>}
        {gateway && (
          <p>
            <span className={gateway.reachable ? "dot ok" : "dot bad"} /> Stream gateway{" "}
            {gateway.reachable ? (
              <>
                <strong>reachable</strong>
                {gateway.version ? ` · go2rtc ${gateway.version}` : ""} ·{" "}
                {gateway.streams.length} stream{gateway.streams.length === 1 ? "" : "s"}
              </>
            ) : (
              <>
                <strong>unreachable</strong>
                {gateway.detail ? ` — ${gateway.detail}` : ""}
              </>
            )}
          </p>
        )}
        {system && system.config_warnings.length > 0 && (
          <ul className="warnings">
            {system.config_warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        )}
        <p className="muted">
          What works today: recording, live view, review timeline and export. Events and
          understanding land in M2–M4
          {system && system.designed_routes.length > 0 && (
            <>
              {" "}
              — still designed:{" "}
              {system.designed_routes
                .map((route) => `${route.path} (${route.milestone})`)
                .join(", ")}
            </>
          )}
          .
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
    </div>
  );
}
