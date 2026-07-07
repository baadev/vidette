import { useEffect, useRef, useState } from "react";
import { api, snapshotUrl, type Camera } from "../api";
import { WhepPlayer, type PlayerState } from "../player";
import "./pages.css";

/** Snapshot refresh cadence while a tile is in snapshot-fallback mode. */
const SNAPSHOT_REFRESH_MS = 2000;

const DOCS_URL = "https://github.com/baadev/vidette/blob/main/docs/getting-started.md";

type TileProps = {
  camera: Camera;
  /** 0-based grid index; indices 0–8 get a 1–9 key hint badge. */
  index: number;
  focused: boolean;
};

/**
 * One camera tile: a WHEP-driven `<video>` that falls back to a periodically
 * refreshed snapshot `<img>` (with a retry button) when live view fails.
 */
function CameraTile({ camera, index, focused }: TileProps) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const [state, setState] = useState<PlayerState>("connecting");
  const [attempt, setAttempt] = useState(0);
  const [snapTick, setSnapTick] = useState(() => Date.now());

  // (Re)start the player when the camera changes or the user retries.
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    setState("connecting");
    const player = new WhepPlayer(video, camera.id);
    player.onstate = setState;
    void player.start();
    return () => {
      player.stop();
    };
  }, [camera.id, attempt]);

  // While failed, refresh the snapshot every couple of seconds.
  useEffect(() => {
    if (state !== "failed") return;
    setSnapTick(Date.now());
    const timer = window.setInterval(() => setSnapTick(Date.now()), SNAPSHOT_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [state]);

  const failed = state === "failed";
  return (
    <figure className={`live-tile${focused ? " live-tile-focused" : ""}`}>
      <div className="live-media">
        <video
          ref={videoRef}
          muted
          autoPlay
          playsInline
          className={failed ? "live-hidden" : undefined}
        />
        {failed && (
          <img
            src={`${snapshotUrl(camera.id)}?t=${snapTick}`}
            alt={`Latest snapshot from ${camera.name}`}
          />
        )}
        {state === "connecting" && <div className="live-overlay">connecting…</div>}
      </div>
      <figcaption className="live-caption">
        {index < 9 && <kbd className="live-key">{index + 1}</kbd>}
        <span className="live-name">{camera.name}</span>
        <span className={`live-state live-state-${state}`}>{state === "live" ? "live" : state}</span>
      </figcaption>
      {failed && (
        <div className="live-fallback">
          <span>Live view unavailable — showing snapshots</span>
          <button type="button" onClick={() => setAttempt((a) => a + 1)}>
            Retry
          </button>
        </div>
      )}
    </figure>
  );
}

/**
 * Live wall: a responsive grid of camera tiles.
 *
 * Keyboard-first: 1–9 focuses a camera full-page, Escape returns to the grid,
 * and arrow keys cycle cameras while in the single-tile view. Tiles (players,
 * snapshot timers) are torn down whenever they unmount — including when
 * switching between grid and single view.
 */
export function LivePage() {
  const [cameras, setCameras] = useState<Camera[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [focus, setFocus] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .cameras()
      .then((cams) => {
        if (!cancelled) setCameras(cams);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          const detail = err instanceof Error ? err.message : String(err);
          setError(
            `Could not load cameras (${detail}). Check that the Vidette server is running, ` +
              "then reload this page.",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      const target = ev.target as HTMLElement | null;
      if (target && /^(INPUT|SELECT|TEXTAREA)$/.test(target.tagName)) return;
      if (!cameras || cameras.length === 0) return;
      const count = cameras.length;

      if (ev.key >= "1" && ev.key <= "9") {
        const idx = Number(ev.key) - 1;
        if (idx < count) {
          setFocus(idx);
          ev.preventDefault();
        }
      } else if (ev.key === "Escape") {
        setFocus(null);
      } else if (focus !== null && (ev.key === "ArrowRight" || ev.key === "ArrowDown")) {
        setFocus((focus + 1) % count);
        ev.preventDefault();
      } else if (focus !== null && (ev.key === "ArrowLeft" || ev.key === "ArrowUp")) {
        setFocus((focus + count - 1) % count);
        ev.preventDefault();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [cameras, focus]);

  // Keep focus index valid if the camera list is shorter than expected.
  const focusedCamera =
    focus !== null && cameras && focus < cameras.length ? cameras[focus] : null;

  return (
    <main className="page live-page">
      <header className="page-header">
        <h1 className="page-title">Live</h1>
        <p className="kbd-hint">
          <kbd>1</kbd>–<kbd>9</kbd> focus a camera · <kbd>Esc</kbd> back to grid ·{" "}
          <kbd>←</kbd>/<kbd>→</kbd> cycle in single view
        </p>
      </header>

      {error && <p className="page-error">{error}</p>}
      {!error && cameras === null && <p className="page-loading">Loading cameras…</p>}

      {cameras !== null && cameras.length === 0 && (
        <div className="empty-state">
          <p>No cameras configured yet.</p>
          <p>
            Add one to your <code>config.yaml</code> and restart the server — the{" "}
            <a href={DOCS_URL} target="_blank" rel="noreferrer">
              getting started guide
            </a>{" "}
            walks through it.
          </p>
        </div>
      )}

      {focusedCamera !== null && (
        <div className="live-single">
          <CameraTile
            key={`single-${focusedCamera.id}`}
            camera={focusedCamera}
            index={focus ?? 0}
            focused
          />
        </div>
      )}

      {focusedCamera === null && cameras !== null && cameras.length > 0 && (
        <div className="live-grid">
          {cameras.map((cam, i) => (
            <CameraTile key={cam.id} camera={cam} index={i} focused={false} />
          ))}
        </div>
      )}
    </main>
  );
}
