import { useEffect, useState } from "react";
import { UNAUTHORIZED_EVENT } from "../api";
import { getPushState, subscribePush, unsubscribePush, type PushState } from "../push";

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

/** Status-line copy per push state; "muted" dots get their color inline. */
const PUSH_STATUS: Record<PushState, { dot: "ok" | "bad" | "muted"; label: string }> = {
  subscribed: { dot: "ok", label: "subscribed on this browser" },
  unsubscribed: { dot: "muted", label: "not subscribed" },
  denied: { dot: "bad", label: "blocked in browser settings" },
  unsupported: { dot: "muted", label: "not supported by this browser" },
};

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
  const [push, setPush] = useState<PushState | null>(null);
  const [pushBusy, setPushBusy] = useState(false);
  const [pushError, setPushError] = useState<string | null>(null);

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
    getPushState()
      .then((state) => {
        if (!cancelled) setPush(state);
      })
      .catch(() => {
        if (!cancelled) setPush("unsupported");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const togglePush = async () => {
    setPushBusy(true);
    setPushError(null);
    try {
      if (push === "subscribed") {
        await unsubscribePush();
      } else {
        await subscribePush();
      }
    } catch (err) {
      setPushError(err instanceof Error ? err.message : String(err));
    } finally {
      // Re-read the real state either way — a failed subscribe may still have
      // flipped the permission (e.g. to "denied").
      setPush(await getPushState().catch(() => "unsupported" as const));
      setPushBusy(false);
    }
  };

  const gateway = system?.gateway;
  const pushStatus = push === null ? null : PUSH_STATUS[push];

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

      <section className="card">
        <h2>Notifications</h2>
        {pushStatus === null && <p className="muted">Checking notification support…</p>}
        {pushStatus !== null && (
          <p>
            <span
              className={pushStatus.dot === "muted" ? "dot" : `dot ${pushStatus.dot}`}
              style={pushStatus.dot === "muted" ? { background: "var(--muted)" } : undefined}
            />{" "}
            Web push <strong>{pushStatus.label}</strong>
          </p>
        )}
        {push !== null && push !== "unsupported" && (
          <p>
            <button
              type="button"
              className="primary"
              onClick={() => void togglePush()}
              disabled={pushBusy || push === "denied"}
            >
              {pushBusy
                ? push === "subscribed"
                  ? "Unsubscribing…"
                  : "Subscribing…"
                : push === "subscribed"
                  ? "Unsubscribe"
                  : "Subscribe"}
            </button>
          </p>
        )}
        {pushError && <p className="error">{pushError}</p>}
        {push === "denied" && (
          <p className="muted">
            The browser is blocking notifications for this site — re-allow them in the site
            settings, then reload this page.
          </p>
        )}
        <p className="muted">
          iOS requires installing Vidette to the Home Screen (Share → Add to Home Screen) before
          push works. Notifications arrive only for confirmed events, per your notification
          rules.
        </p>
      </section>

      <footer>
        <a href="https://github.com/baadev/vidette">GitHub</a>
        <a href="https://github.com/baadev/vidette/blob/main/ROADMAP.md">Roadmap</a>
        <a href="/api/docs">API docs</a>
        <a href="mailto:alex@baadev.com">Contact</a>
      </footer>
      <p className="muted" style={{ margin: 0, textAlign: "center" }}>
        <a
          href="/metrics"
          style={{ color: "inherit", textDecoration: "none", borderBottom: "1px solid var(--line)" }}
        >
          Prometheus metrics
        </a>{" "}
        — scrape with a bearer token.
      </p>
    </div>
  );
}
