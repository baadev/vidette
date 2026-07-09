import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type Camera, type EventGeometry, type EventInfo } from "../api";
import "./pages.css";

/** Feed refresh cadence while the tab is visible. */
const REFRESH_MS = 10_000;
const EVENT_LIMIT = 50;
const DAY_SECONDS = 86_400;

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** Local wall-clock time (HH:MM:SS) of an epoch timestamp. */
function formatLocalClock(ts: number): string {
  const d = new Date(ts * 1000);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

/** Local calendar day of an epoch timestamp, as a stable "YYYY-MM-DD" key. */
function localDayKey(ts: number): string {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

function localDayLabel(ts: number): string {
  const nowTs = Date.now() / 1000;
  const key = localDayKey(ts);
  if (key === localDayKey(nowTs)) return "Today";
  if (key === localDayKey(nowTs - DAY_SECONDS)) return "Yesterday";
  return new Date(ts * 1000).toLocaleDateString(undefined, {
    weekday: "long",
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

function describeError(err: unknown, doing: string): string {
  const detail = err instanceof Error ? err.message : String(err);
  return `Could not ${doing} (${detail}). Check that the Vidette server is running, then retry.`;
}

/** Geometry chips shown only when they actually say something. */
function geometryBadges(geometry: EventGeometry): string[] {
  const badges: string[] = [];
  if (geometry.touch) badges.push("touched");
  if (geometry.loiter) badges.push("loitering");
  if (geometry.repeat_pass > 0) badges.push(`×${geometry.repeat_pass} passes`);
  if (geometry.dwell_s !== null && geometry.dwell_s >= 3) {
    badges.push(`${Math.round(geometry.dwell_s)}s dwell`);
  }
  return badges;
}

type EventCardProps = {
  event: EventInfo;
  cameraName: string;
  expanded: boolean;
  onToggle: () => void;
  /** Effective verdict: optimistic local vote or the server-recorded one. */
  feedback: "up" | "down" | null;
  onVote: (verdict: "up" | "down") => void;
};

/**
 * One event in the feed. The main area is a button that expands an inline clip
 * player; the thumbs live outside it (no nested buttons) and lock in after one
 * vote. Snapshot/clip may 404 while footage is still only on disk — both fall
 * back gracefully instead of pretending.
 */
function EventCard({ event, cameraName, expanded, onToggle, feedback, onVote }: EventCardProps) {
  const [thumbFailed, setThumbFailed] = useState(false);
  const [clipFailed, setClipFailed] = useState(false);

  // Re-expanding retries the clip — it may have landed since the last attempt.
  useEffect(() => {
    if (expanded) setClipFailed(false);
  }, [expanded]);

  const timeRange =
    event.ended_at !== null
      ? `${formatLocalClock(event.started_at)}–${formatLocalClock(event.ended_at)}`
      : `${formatLocalClock(event.started_at)} · ongoing`;
  const badges = geometryBadges(event.geometry);
  const showThumb = event.snapshot !== null && !thumbFailed;

  return (
    <li className={`event-card${expanded ? " event-card-expanded" : ""}`}>
      <button type="button" className="event-card-main" onClick={onToggle} aria-expanded={expanded}>
        <div className="event-thumb">
          {showThumb ? (
            <img
              src={event.snapshot ?? undefined}
              alt=""
              loading="lazy"
              onError={() => setThumbFailed(true)}
            />
          ) : (
            <span className="event-thumb-empty">no snapshot</span>
          )}
        </div>
        <div className="event-body">
          <div className="event-head">
            <span className="event-camera">{cameraName}</span>
            <span className="event-time">{timeRange}</span>
            <span className={`event-state event-state-${event.state}`}>{event.state}</span>
          </div>
          {(event.kinds.length > 0 || event.zones.length > 0 || badges.length > 0) && (
            <div className="event-chips">
              {event.kinds.map((kind) => (
                <span key={`kind-${kind}`} className="chip">
                  {kind}
                </span>
              ))}
              {event.zones.map((zone) => (
                <span key={`zone-${zone}`} className="chip chip-zone">
                  {zone}
                </span>
              ))}
              {badges.map((badge) => (
                <span key={`geo-${badge}`} className="chip chip-geo">
                  {badge}
                </span>
              ))}
            </div>
          )}
          {event.summary && <p className="event-summary">{event.summary}</p>}
        </div>
      </button>
      <div className="event-feedback">
        <button
          type="button"
          className={`feedback-button${feedback === "up" ? " feedback-active" : ""}`}
          disabled={feedback !== null}
          onClick={() => onVote("up")}
          aria-label="Useful alert"
          title="Useful alert"
        >
          👍
        </button>
        <button
          type="button"
          className={`feedback-button${feedback === "down" ? " feedback-active" : ""}`}
          disabled={feedback !== null}
          onClick={() => onVote("down")}
          aria-label="Not useful"
          title="Not useful"
        >
          👎
        </button>
      </div>
      {expanded && (
        <div className="event-detail">
          {clipFailed ? (
            <p className="muted">clip not ready yet — footage may still be on disk only</p>
          ) : (
            <video
              className="event-clip"
              controls
              preload="none"
              src={event.clip}
              onError={() => setClipFailed(true)}
            />
          )}
        </div>
      )}
    </li>
  );
}

/**
 * Events feed (M2): what the cascade understood, newest first, grouped by local
 * day. Refreshes every 10 s while the tab is visible and pauses when it is not;
 * refreshes keep stable card keys so the layout never jumps under the cursor.
 */
export function EventsPage() {
  const [cameras, setCameras] = useState<Camera[] | null>(null);
  const [cameraFilter, setCameraFilter] = useState("");
  const [events, setEvents] = useState<EventInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [votes, setVotes] = useState<Record<string, "up" | "down">>({});
  const [voteError, setVoteError] = useState<string | null>(null);

  // Load cameras once — for the filter select and to show names instead of ids.
  useEffect(() => {
    let cancelled = false;
    api
      .cameras()
      .then((cams) => {
        if (!cancelled) setCameras(cams);
      })
      .catch(() => {
        // The feed still works with raw camera ids; the filter just stays empty.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Load the feed, then keep it fresh — but only while the tab is visible.
  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;
    setEvents(null);
    setError(null);
    setExpandedId(null);

    const refresh = () => {
      const opts: { camera?: string; limit?: number } = { limit: EVENT_LIMIT };
      if (cameraFilter) opts.camera = cameraFilter;
      api
        .events(opts)
        .then((list) => {
          if (cancelled) return;
          setEvents(list);
          setError(null);
        })
        .catch((err: unknown) => {
          if (!cancelled) setError(describeError(err, "load events"));
        });
    };
    const start = () => {
      if (timer === null) timer = window.setInterval(refresh, REFRESH_MS);
    };
    const stop = () => {
      if (timer !== null) {
        window.clearInterval(timer);
        timer = null;
      }
    };
    const onVisibility = () => {
      if (document.hidden) {
        stop();
      } else {
        refresh();
        start();
      }
    };

    refresh();
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      cancelled = true;
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [cameraFilter]);

  const vote = useCallback((id: string, verdict: "up" | "down") => {
    setVoteError(null);
    setVotes((prev) => ({ ...prev, [id]: verdict })); // optimistic — reverted on failure
    api.eventFeedback(id, verdict).catch((err: unknown) => {
      setVotes((prev) => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
      setVoteError(describeError(err, "record your feedback"));
    });
  }, []);

  const cameraNames = useMemo(
    () => new Map((cameras ?? []).map((cam) => [cam.id, cam.name])),
    [cameras],
  );

  // Group the (newest-first) feed into runs of the same local day.
  const groups = useMemo(() => {
    const out: { key: string; label: string; events: EventInfo[] }[] = [];
    for (const event of events ?? []) {
      const key = localDayKey(event.started_at);
      const last = out[out.length - 1];
      if (last && last.key === key) {
        last.events.push(event);
      } else {
        out.push({ key, label: localDayLabel(event.started_at), events: [event] });
      }
    }
    return out;
  }, [events]);

  return (
    <main className="page events-page">
      <header className="page-header">
        <h1 className="page-title">Events</h1>
        <p className="kbd-hint">Refreshes every 10 s while this tab is visible.</p>
      </header>

      <div className="feed-controls">
        <label>
          Camera{" "}
          <select value={cameraFilter} onChange={(ev) => setCameraFilter(ev.target.value)}>
            <option value="">All cameras</option>
            {(cameras ?? []).map((cam) => (
              <option key={cam.id} value={cam.id}>
                {cam.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      {error && <p className="page-error">{error}</p>}
      {voteError && <p className="page-error">{voteError}</p>}
      {!error && events === null && <p className="page-loading">Loading events…</p>}

      {events !== null && events.length === 0 && (
        <div className="empty-state">
          <p>Quiet. That&rsquo;s the product working.</p>
          <p>
            Events appear once detection is on — enable <code>detect.enabled</code> and draw
            zones for your cameras
            {cameraFilter ? ", or clear the camera filter above" : ""}.
          </p>
        </div>
      )}

      {groups.map((group) => (
        <section key={group.key} className="event-day">
          <h2 className="event-day-label">{group.label}</h2>
          <ul className="event-list">
            {group.events.map((event) => (
              <EventCard
                key={event.id}
                event={event}
                cameraName={cameraNames.get(event.camera) ?? event.camera}
                expanded={expandedId === event.id}
                onToggle={() =>
                  setExpandedId((current) => (current === event.id ? null : event.id))
                }
                feedback={votes[event.id] ?? event.feedback}
                onVote={(verdict) => vote(event.id, verdict)}
              />
            ))}
          </ul>
        </section>
      ))}
    </main>
  );
}
