import { useEffect, useState } from "react";
import { UNAUTHORIZED_EVENT } from "../api";
import { getPushState, subscribePush, unsubscribePush, type PushState } from "../push";
import { CalibrationCard } from "./CalibrationCard";
import { VerdictsCard } from "./VerdictsCard";

type Health = { status: string; version: string };

type FrigateInfo = {
  url: string;
  broker: string;
  connected: boolean;
  available: boolean | null;
  messages_total: number;
  invalid_total: number;
  incidents_total: number;
  last_message_at: number | null;
};

type ClipFolderInfo = {
  path: string;
  camera: string;
  exists: boolean;
  clips_total: number;
  errors_total: number;
  last_clip_at: number | null;
};

type SystemInfo = {
  milestone: string;
  config_warnings: string[];
  auth_mode: string;
  frigate: FrigateInfo | null;
  clip_folders: ClipFolderInfo[];
  designed_routes: { path: string; milestone: string }[];
};

// Since v0.2 (ADR-0009) the cheap tiers run in Frigate (or in the camera itself, for a
// clip folder, ADR-0010); Vidette owns the last two.
const TIERS = [
  { name: "T0 · Motion", detail: "Frigate, or the camera's own trigger", milestone: "upstream" },
  { name: "T1 · Detection", detail: "Frigate", milestone: "upstream" },
  { name: "T2 · Tracking + zones", detail: "Frigate review items", milestone: "upstream" },
  { name: "T3 · Scene verdict", detail: "VLM, local or cloud, budgeted", milestone: "Vidette" },
  { name: "T4 · Your policy", detail: "plain language, measured with vidette eval", milestone: "Vidette" },
];

function ago(ts: number): string {
  const minutes = Math.round((Date.now() / 1000 - ts) / 60);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return hours < 48 ? `${hours} h ago` : `${Math.round(hours / 24)} days ago`;
}

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

  const pushStatus = push === null ? null : PUSH_STATUS[push];

  return (
    <div className="page-narrow">
      <section className="card status">
        <h2>System</h2>
        {error && <p className="error">{error}</p>}
        {health && (
          <p>
            <span className="dot ok" /> API <strong>{health.status}</strong> · v{health.version} ·
            <strong>{system?.milestone ?? "…"}</strong>
          </p>
        )}
        {!health && !error && <p>Contacting API…</p>}
        {system && system.frigate === null && system.clip_folders.length === 0 && (
          <p>
            <span className="dot bad" /> <strong>No incident source</strong>: add a{" "}
            <code>frigate:</code> or <code>clip_folders:</code> section to receive incidents
          </p>
        )}
        {system?.clip_folders.map((folder) => (
          <p key={folder.path} className="source-line">
            <span className={folder.exists ? "dot ok" : "dot bad"} /> Clip folder{" "}
            <code className="path">{folder.path}</code>{" "}
            {folder.exists ? (
              <>
                · {folder.clips_total} new clip{folder.clips_total === 1 ? "" : "s"} since start
                {folder.last_clip_at !== null ? `, last ${ago(folder.last_clip_at)}` : ""}
                {folder.errors_total > 0 ? `, ${folder.errors_total} unreadable` : ""}
              </>
            ) : (
              <strong>not found</strong>
            )}
          </p>
        ))}
        {system?.frigate && (
          <p>
            <span className={system.frigate.connected ? "dot ok" : "dot bad"} /> Frigate broker{" "}
            <strong>{system.frigate.connected ? "connected" : "not connected"}</strong> (
            {system.frigate.broker}) · Frigate{" "}
            {system.frigate.available === null
              ? "availability unknown"
              : system.frigate.available
                ? "online"
                : "offline"}{" "}
            · {system.frigate.messages_total} messages, {system.frigate.incidents_total} incidents
            {system.frigate.invalid_total > 0 ? `, ${system.frigate.invalid_total} unreadable` : ""}
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
          What works today: incidents from Frigate or a clip folder land in the Events feed;
          with a VLM selected below, each closed incident is judged against your policies and
          only an alert verdict notifies. Policies live in the config file
          {system && system.designed_routes.length > 0 && (
            <>
              {" "}
              (still designed:{" "}
              {system.designed_routes
                .map((route) => `${route.path} (${route.milestone})`)
                .join(", ")}
              )
            </>
          )}
          .
        </p>
      </section>

      <VerdictsCard />
      <CalibrationCard />

      <section className="card">
        <h2>Who does what</h2>
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
          push works. Notifications arrive only for confirmed events (an alert verdict), per
          your notification rules.
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
