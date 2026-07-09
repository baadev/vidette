import { useCallback, useEffect, useState } from "react";
import { api, snapshotUrl, type Camera } from "../api";
import "./pages.css";

const DOCS_URL = "https://github.com/baadev/vidette/blob/main/docs/getting-started.md";

// Matches the shape enforced by the config schema (see deploy/config.example.yaml).
const YAML_SNIPPET = `cameras:
  front-door:
    adapter: rtsp
    name: "Front door"
    source:
      main: rtsp://user:\${CAM_PASSWORD}@192.168.1.20:554/stream1`;

function describeError(err: unknown, doing: string): string {
  const detail = err instanceof Error ? err.message : String(err);
  return `Could not ${doing} (${detail}). Check that the Vidette server is running, then retry.`;
}

type CopyState = "idle" | "copied" | "failed";

type SetupCameraRowProps = {
  camera: Camera;
  /** Re-fetches the camera list so the stream/recorder status stays honest. */
  onRecheck: () => void;
};

/**
 * One checklist row: snapshot thumbnail (placeholder until the stream produces
 * one), adapter chip, stream-ready dot and recorder state. Refresh re-requests
 * the snapshot and rechecks the camera list.
 */
function SetupCameraRow({ camera, onRecheck }: SetupCameraRowProps) {
  const [snapTick, setSnapTick] = useState(() => Date.now());
  const [snapFailed, setSnapFailed] = useState(false);

  const refresh = () => {
    setSnapFailed(false);
    setSnapTick(Date.now());
    onRecheck();
  };

  return (
    <li className="setup-row">
      <div className="setup-thumb">
        {snapFailed ? (
          <span className="setup-thumb-empty">no snapshot yet</span>
        ) : (
          <img
            src={`${snapshotUrl(camera.id)}?t=${snapTick}`}
            alt={`Latest snapshot from ${camera.name}`}
            onError={() => setSnapFailed(true)}
          />
        )}
      </div>
      <div className="setup-row-info">
        <div className="setup-row-name">
          <span className="setup-name">{camera.name}</span>
          <span className="chip">{camera.adapter}</span>
        </div>
        <div className="setup-row-status">
          <span>
            <span className={camera.stream_ready ? "dot ok" : "dot bad"} />
            stream {camera.stream_ready ? "ready" : "not ready"}
          </span>
          <span className="muted">recorder: {camera.state}</span>
        </div>
      </div>
      <button type="button" className="ghost" onClick={refresh}>
        Refresh
      </button>
    </li>
  );
}

export type SetupPageProps = {
  onDone: () => void;
};

/**
 * First-run wizard, step 2: connect cameras. Shown once right after the admin
 * account is created (and reachable any time via #/setup — it is deliberately
 * not part of the nav). With no cameras it explains where the config lives and
 * how to add one; with cameras it shows an honest per-camera checklist.
 */
export function SetupPage({ onDone }: SetupPageProps) {
  const [cameras, setCameras] = useState<Camera[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [copyState, setCopyState] = useState<CopyState>("idle");

  const recheck = useCallback(() => {
    setChecking(true);
    api
      .cameras()
      .then((cams) => {
        setCameras(cams);
        setError(null);
      })
      .catch((err: unknown) => setError(describeError(err, "check for cameras")))
      .finally(() => setChecking(false));
  }, []);

  useEffect(() => {
    recheck();
  }, [recheck]);

  // Let the copy-button label fall back to "Copy" after a moment.
  useEffect(() => {
    if (copyState === "idle") return;
    const timer = window.setTimeout(() => setCopyState("idle"), 2000);
    return () => window.clearTimeout(timer);
  }, [copyState]);

  const copySnippet = () => {
    navigator.clipboard
      .writeText(YAML_SNIPPET)
      .then(() => setCopyState("copied"))
      .catch(() => setCopyState("failed"));
  };

  const copyLabel =
    copyState === "copied" ? "Copied" : copyState === "failed" ? "Copy failed — select it" : "Copy";

  return (
    <div className="page-narrow">
      <header className="page-header">
        <h1 className="page-title">Setup</h1>
        <p className="kbd-hint">Step 2 of 2 — connect your cameras.</p>
      </header>

      {error && <p className="page-error">{error}</p>}
      {!error && cameras === null && <p className="page-loading">Checking for cameras…</p>}

      {cameras !== null && cameras.length === 0 && (
        <section className="card setup-card">
          <h2>Add your first camera</h2>
          <p>
            No cameras are configured yet. Cameras live in the server config at{" "}
            <code>/config/vidette.yaml</code> (the <code>config/</code> volume if you run the
            container). Any RTSP camera works — add a block like this:
          </p>
          <div className="setup-snippet">
            <pre className="setup-yaml">
              <code>{YAML_SNIPPET}</code>
            </pre>
            <button type="button" className="ghost setup-copy" onClick={copySnippet}>
              {copyLabel}
            </button>
          </div>
          <p className="hint">
            Check the file with <code>vidette validate /config/vidette.yaml</code>, restart the
            server, then recheck below. The{" "}
            <a href={DOCS_URL} target="_blank" rel="noreferrer">
              getting started guide
            </a>{" "}
            covers vendor-specific stream URLs.
          </p>
          <div className="setup-actions">
            <button
              type="button"
              className="primary"
              onClick={recheck}
              disabled={checking}
            >
              {checking ? "Rechecking…" : "Recheck"}
            </button>
          </div>
        </section>
      )}

      {cameras !== null && cameras.length > 0 && (
        <>
          <section className="card setup-card">
            <div className="setup-check-head">
              <h2>Camera checklist</h2>
              <button type="button" className="ghost" onClick={recheck} disabled={checking}>
                {checking ? "Rechecking…" : "Recheck"}
              </button>
            </div>
            <p className="muted">
              Each camera should show a green stream dot and a snapshot. If one stays red,
              double-check its stream URL and credentials in <code>/config/vidette.yaml</code>,
              then restart the server.
            </p>
            <ul className="setup-list">
              {cameras.map((cam) => (
                <SetupCameraRow key={cam.id} camera={cam} onRecheck={recheck} />
              ))}
            </ul>
          </section>
          <div className="setup-footer">
            <button type="button" className="primary" onClick={onDone}>
              Everything looks good — go to Live
            </button>
          </div>
        </>
      )}
    </div>
  );
}
