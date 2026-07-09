import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  UNAUTHORIZED_EVENT,
  api,
  wsUrl,
  type Camera,
  type EventGeometry,
  type EventInfo,
} from "../api";
import "./pages.css";

/** Fallback feed poll cadence — used only while the live socket is down. */
const REFRESH_MS = 10_000;
const EVENT_LIMIT = 50;
const DAY_SECONDS = 86_400;
/** Live socket: coalesce event-driven refetches to at most one per window… */
const WS_REFETCH_MIN_MS = 2_000;
/** …and reconnect with exponential backoff between these bounds. */
const WS_BACKOFF_MIN_MS = 1_000;
const WS_BACKOFF_MAX_MS = 30_000;

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
  /** Toggle the star — the caller owns the optimistic flip and its revert. */
  onFavorite: () => void;
};

/**
 * One event in the feed. The main area is a button that expands an inline clip
 * player; the star and thumbs live outside it (no nested buttons) — the star
 * toggles freely, the thumbs lock in after one vote. Snapshot/clip may 404
 * while footage is still only on disk — both fall back gracefully instead of
 * pretending.
 */
function EventCard({
  event,
  cameraName,
  expanded,
  onToggle,
  feedback,
  onVote,
  onFavorite,
}: EventCardProps) {
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
          {/* The stylesheet reserves room for two feedback buttons; the star
              makes three, so the top row carries a little extra clearance. */}
          <div className="event-head" style={{ paddingRight: "2.25rem" }}>
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
          className={`feedback-button${event.favorite ? " feedback-active" : ""}`}
          onClick={onFavorite}
          aria-pressed={event.favorite}
          aria-label={event.favorite ? "Remove from favorites" : "Add to favorites"}
          title={event.favorite ? "Remove from favorites" : "Add to favorites"}
          style={event.favorite ? { color: "var(--accent)" } : undefined}
        >
          {event.favorite ? "★" : "☆"}
        </button>
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
 * day. Live over the server's WebSocket — confirmed/ended events nudge a
 * coalesced refetch — with the 10 s poll kept as a fallback that stands down
 * while the socket is open (and pauses while the tab is hidden). Refreshes keep
 * stable card keys so the layout never jumps under the cursor.
 */
export function EventsPage() {
  const [cameras, setCameras] = useState<Camera[] | null>(null);
  const [cameraFilter, setCameraFilter] = useState("");
  const [favoriteOnly, setFavoriteOnly] = useState(false);
  const [events, setEvents] = useState<EventInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [votes, setVotes] = useState<Record<string, "up" | "down">>({});
  const [voteError, setVoteError] = useState<string | null>(null);
  const [favoriteError, setFavoriteError] = useState<string | null>(null);
  const [wsLive, setWsLive] = useState(false);
  // Mirrors `wsLive` for the poll tick, which must not re-arm the interval.
  const wsLiveRef = useRef(false);
  // Latest feed refresh, so the (mount-once) socket always refetches with the
  // current filters. Reset to a no-op on cleanup.
  const refreshRef = useRef<() => void>(() => {});

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

  // Load the feed, then keep it fresh. The 10 s poll is the fallback: it only
  // fetches while the socket is down, and only while the tab is visible.
  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;
    setEvents(null);
    setError(null);
    setExpandedId(null);

    const refresh = () => {
      const opts: { camera?: string; limit?: number; favoriteOnly?: boolean } = {
        limit: EVENT_LIMIT,
      };
      if (cameraFilter) opts.camera = cameraFilter;
      if (favoriteOnly) opts.favoriteOnly = true;
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
    refreshRef.current = refresh;

    const tick = () => {
      if (!wsLiveRef.current) refresh();
    };
    const start = () => {
      if (timer === null) timer = window.setInterval(tick, REFRESH_MS);
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
      refreshRef.current = () => {};
    };
  }, [cameraFilter, favoriteOnly]);

  // One socket for the page's lifetime. Confirmed/ended events nudge a refetch
  // (coalesced to at most one per WS_REFETCH_MIN_MS); the header dot tells the
  // truth about the connection. Reconnects back off 1 s → 30 s. A 4401 close
  // means the session is gone — defer to the app-wide login fallback instead
  // of hammering the server.
  useEffect(() => {
    let disposed = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | null = null;
    let refetchTimer: number | null = null;
    let backoffMs = WS_BACKOFF_MIN_MS;
    let lastRefetchAt = Date.now(); // the feed effect already fetched on mount

    const setLive = (live: boolean) => {
      wsLiveRef.current = live;
      setWsLive(live);
    };

    const scheduleRefetch = () => {
      if (refetchTimer !== null) return; // one pending refetch covers the burst
      const wait = Math.max(0, lastRefetchAt + WS_REFETCH_MIN_MS - Date.now());
      refetchTimer = window.setTimeout(() => {
        refetchTimer = null;
        lastRefetchAt = Date.now();
        refreshRef.current();
      }, wait);
    };

    const scheduleReconnect = () => {
      if (disposed || reconnectTimer !== null) return;
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, backoffMs);
      backoffMs = Math.min(backoffMs * 2, WS_BACKOFF_MAX_MS);
    };

    const connect = () => {
      if (disposed) return;
      let ws: WebSocket;
      try {
        ws = new WebSocket(wsUrl());
      } catch {
        scheduleReconnect();
        return;
      }
      socket = ws;
      ws.onopen = () => {
        if (disposed) return;
        backoffMs = WS_BACKOFF_MIN_MS;
        setLive(true);
        scheduleRefetch(); // catch up on anything that happened while offline
      };
      ws.onmessage = (message: MessageEvent) => {
        if (disposed || typeof message.data !== "string") return;
        let topic: unknown;
        try {
          topic = (JSON.parse(message.data) as { topic?: unknown }).topic;
        } catch {
          return; // not our JSON — ignore rather than guess
        }
        if (topic === "event.confirmed" || topic === "event.ended") scheduleRefetch();
      };
      ws.onerror = () => {
        ws.close(); // the close handler owns reconnection
      };
      ws.onclose = (closed: CloseEvent) => {
        if (socket === ws) socket = null;
        if (disposed) return;
        setLive(false);
        if (closed.code === 4401) {
          // Unauthenticated — the shell swaps to the login screen; no reconnect loop.
          window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
          return;
        }
        scheduleReconnect();
      };
    };

    connect();
    return () => {
      disposed = true;
      wsLiveRef.current = false;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      if (refetchTimer !== null) window.clearTimeout(refetchTimer);
      socket?.close();
    };
  }, []);

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

  const toggleFavorite = useCallback((id: string, favorite: boolean) => {
    setFavoriteError(null);
    const apply = (value: boolean) =>
      setEvents((current) =>
        current === null
          ? current
          : current.map((event) => (event.id === id ? { ...event, favorite: value } : event)),
      );
    apply(favorite); // optimistic — reverted on failure
    api.setEventFavorite(id, favorite).catch((err: unknown) => {
      apply(!favorite);
      setFavoriteError(describeError(err, favorite ? "star the event" : "unstar the event"));
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
        <p className="kbd-hint">
          <span
            className="dot"
            style={{ background: wsLive ? "var(--ok)" : "var(--muted)" }}
            aria-hidden="true"
          />
          {wsLive
            ? "live — new events appear as they happen"
            : "connecting — refreshing every 10 s until live"}
        </p>
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
        <button
          type="button"
          className="ghost"
          onClick={() => setFavoriteOnly((current) => !current)}
          aria-pressed={favoriteOnly}
          title={favoriteOnly ? "Show all events" : "Show only starred events"}
          style={favoriteOnly ? { color: "var(--accent)", borderColor: "var(--accent)" } : undefined}
        >
          ★ only
        </button>
      </div>

      {error && <p className="page-error">{error}</p>}
      {voteError && <p className="page-error">{voteError}</p>}
      {favoriteError && <p className="page-error">{favoriteError}</p>}
      {!error && events === null && <p className="page-loading">Loading events…</p>}

      {events !== null && events.length === 0 && (
        <div className="empty-state">
          {favoriteOnly ? (
            <p>
              No starred events{cameraFilter ? " for this camera" : ""} yet — tap ☆ on an event
              to keep it here.
            </p>
          ) : (
            <>
              <p>Quiet. That&rsquo;s the product working.</p>
              <p>
                Events appear once detection is on — enable <code>detect.enabled</code> and draw
                zones for your cameras
                {cameraFilter ? ", or clear the camera filter above" : ""}.
              </p>
            </>
          )}
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
                onFavorite={() => toggleFavorite(event.id, !event.favorite)}
              />
            ))}
          </ul>
        </section>
      ))}
    </main>
  );
}
