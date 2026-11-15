import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  UNAUTHORIZED_EVENT,
  api,
  wsUrl,
  type ConfirmResult,
  type EventGeometry,
  type EventInfo,
  type EventPeople,
  type Identity,
  type SeenPerson,
  type Linked,
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

type Enrolled = { id: string; name: string };

const NEAR_KNOWN = 0.3; // people.identities.near_known's default (docs/people.md)

const STATUS_WORD: Record<string, string> = {
  familiar: "familiar",
  suspicious: "suspicious",
  known: "known",
};

function identityLabel(seen: SeenPerson): string | null {
  if (!seen.identity) return null;
  const name = seen.identity_name ? `${seen.identity_name} · ` : "";
  return `${name}#${seen.identity}`;
}

function answerLabel(seen: SeenPerson, enrolled: Enrolled[]): string {
  const answer = seen.confirmed ?? "";
  if (answer === "stranger") return seen.identity ? `a stranger (#${seen.identity})` : "a stranger";
  if (answer === "unsure") return "not sure";
  if (answer.startsWith("identity:")) {
    const status = seen.identity_status ? STATUS_WORD[seen.identity_status] : undefined;
    return `${identityLabel(seen) ?? answer}${status ? ` (${status})` : ""}`;
  }
  return `this is ${enrolled.find((person) => person.id === answer)?.name ?? answer}`;
}

type FaceRowProps = {
  eventId: string;
  seen: SeenPerson;
  enrolled: Enrolled[];
  onChanged: (people: EventPeople) => void;
};

type Naming = null | "familiar" | "unknown";

/**
 * One face seen in an event: its crop, who it matched, and every answer about it. Each
 * answer can be changed or taken back (ADR-0016): "This is Sam" adds the crop to Sam's
 * references, "Stranger" keeps it as an anonymous identity, "Familiar…" names it as a
 * neighbour who should not wake anyone, "Add to…" puts it in an existing identity.
 */
function FaceRow({ eventId, seen, enrolled, onChanged }: FaceRowProps) {
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(!seen.confirmed);
  const [naming, setNaming] = useState<Naming>(null);
  const [name, setName] = useState("");
  const [choices, setChoices] = useState<Identity[] | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async (action: () => Promise<ConfirmResult>) => {
    setBusy(true);
    setError(null);
    try {
      const result = await action();
      setMessage(result.message);
      onChanged(result.people);
      setOpen(false);
      setNaming(null);
      setName("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  const answer = (value: string) => run(() => api.confirmEventFace(eventId, seen.n, value));
  const toIdentity = (choice: { identity?: number; name?: string; status?: "unknown" | "familiar" }) =>
    run(() => api.faceIdentity(eventId, seen.n, choice));
  const loadChoices = () => {
    if (choices !== null) return;
    api
      .identities()
      .then((state) => setChoices(state.identities.filter((identity) => identity.status !== "known")))
      .catch(() => setChoices([]));
  };

  let label: string;
  if (seen.status === "known") label = `${seen.name ?? seen.person} ${seen.score.toFixed(2)}`;
  else if (seen.identity) label = seen.identity_name ?? `Stranger #${seen.identity}`;
  else label = seen.status === "unclear" ? "Small face" : "Unknown face";
  const disposition = seen.identity_status && STATUS_WORD[seen.identity_status];
  // the nearest enrolled person is a suggestion only when the face came close (near_known)
  const closest =
    seen.closest && seen.score >= NEAR_KNOWN
      ? enrolled.find((person) => person.id === seen.closest)
      : undefined;
  return (
    <li className="face-row">
      {seen.crop ? (
        <img className="face-crop" src={seen.crop} alt="" loading="lazy" />
      ) : (
        <span className="face-crop face-crop-empty" aria-hidden="true" />
      )}
      <div className="face-info">
        <span className={`face-label${seen.status === "known" ? " face-known" : ""}`}>
          {seen.identity ? <a href={`#/identities/${seen.identity}`}>{label}</a> : label}
          {seen.identity && seen.identity_name ? <span className="muted"> #{seen.identity}</span> : null}
          {disposition && seen.identity_status !== "known" ? (
            <span className={`id-status id-status-${seen.identity_status}`}>{disposition}</span>
          ) : null}
        </span>
        <span className="muted face-meta">
          {seen.faces} frame{seen.faces === 1 ? "" : "s"} · {seen.face_px}px
          {seen.identity_note ? ` · ${seen.identity_note}` : ""}
        </span>
        {seen.small && (
          <span className="muted face-meta face-small">
            Small face: it may not be recognized next time.
          </span>
        )}
        {seen.confirmed && (
          <span className="face-decided">
            <span className="face-confirmed">✓ {answerLabel(seen, enrolled)}</span>
            <button type="button" className="ghost" disabled={busy} onClick={() => setOpen((on) => !on)}>
              {open ? "Close" : "Change"}
            </button>
            <button
              type="button"
              className="ghost"
              disabled={busy}
              onClick={() => void run(() => api.undoFace(eventId, seen.n))}
            >
              Undo
            </button>
          </span>
        )}
        {open && (
          <div className="face-answers" role="group" aria-label="Who is this?">
            {enrolled.map((person) => (
              <button
                key={person.id}
                type="button"
                className={`ghost${seen.person === person.id || closest?.id === person.id ? " face-suggested" : ""}`}
                disabled={busy}
                onClick={() => void answer(person.id)}
              >
                This is {person.name}
              </button>
            ))}
            <button type="button" className="ghost" disabled={busy} onClick={() => void answer("stranger")}>
              Stranger
            </button>
            <button type="button" className="ghost" disabled={busy} onClick={() => void answer("unsure")}>
              Not sure
            </button>
            <button
              type="button"
              className={`ghost${naming === "familiar" ? " face-suggested" : ""}`}
              disabled={busy}
              onClick={() => setNaming(naming === "familiar" ? null : "familiar")}
            >
              Familiar…
            </button>
            <button
              type="button"
              className={`ghost${naming === "unknown" ? " face-suggested" : ""}`}
              disabled={busy}
              onClick={() => setNaming(naming === "unknown" ? null : "unknown")}
            >
              New identity…
            </button>
            <select
              className="face-pick"
              aria-label="Add to an identity"
              disabled={busy}
              value=""
              onFocus={loadChoices}
              onPointerDown={loadChoices}
              onChange={(event) => {
                const id = Number(event.target.value);
                if (id) void toIdentity({ identity: id });
              }}
            >
              <option value="">Add to…</option>
              {(choices ?? []).map((identity) => (
                <option key={identity.id} value={identity.id}>
                  {identity.label}
                  {identity.status !== "unknown" ? ` (${identity.status})` : ""} · ×{identity.sightings}
                </option>
              ))}
            </select>
          </div>
        )}
        {open && naming && (
          <form
            className="face-name"
            onSubmit={(event) => {
              event.preventDefault();
              void toIdentity({ name: name.trim() || undefined, status: naming });
            }}
          >
            <input
              value={name}
              maxLength={40}
              autoFocus
              placeholder={naming === "familiar" ? "Who is it? e.g. Neighbour" : "Name (optional)"}
              aria-label="Name"
              onChange={(event) => setName(event.target.value)}
            />
            <button type="submit" className="primary" disabled={busy || (naming === "familiar" && !name.trim())}>
              {naming === "familiar" ? "Save familiar" : "Create"}
            </button>
            {naming === "familiar" && (
              <span className="muted face-meta">
                Familiar people do not wake anyone, like the household; critical events still do.
              </span>
            )}
          </form>
        )}
        {message && <span className="muted face-meta">{message}</span>}
        {error && <span className="error face-meta">{error}</span>}
      </div>
    </li>
  );
}

type EventCardProps = {
  event: EventInfo;
  /** People enrolled for face confirmation; empty when known people are off. */
  enrolled: Enrolled[];
  onPeopleChanged: (people: EventPeople) => void;
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
  enrolled,
  onPeopleChanged,
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
            <span className="event-camera">{event.camera}</span>
            <span className="event-time">{timeRange}</span>
            <span className={`event-state event-state-${event.state}`}>{event.state}</span>
          </div>
          {(event.kinds.length > 0 ||
            event.zones.length > 0 ||
            badges.length > 0 ||
            Boolean(event.upstream?.severity)) && (
            <div className="event-chips">
              {event.upstream?.severity && (
                <span className="chip" title="Frigate's own triage">
                  frigate {event.upstream.severity}
                </span>
              )}
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
          {event.people && event.people.seen.length === 0 && event.people.frames_checked > 0 && (
            <p className="event-summary muted">
              {event.people.unclear_frames > 0
                ? `No face to link: ${event.people.unclear_frames} frame${event.people.unclear_frames === 1 ? "" : "s"} with a face too small or brief to follow`
                : "No face found: nobody can be linked to an identity from this clip"}
            </p>
          )}
          {event.summary && <p className="event-summary">{event.summary}</p>}
          {!event.summary && event.upstream?.summary?.text && (
            <p className="event-summary muted">Frigate: {event.upstream.summary.text}</p>
          )}
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
      {event.people && event.people.seen.length > 0 && (
        <ul className="event-faces" aria-label="Faces seen">
          {event.people.seen.map((seen) => (
            <FaceRow
              key={seen.n}
              eventId={event.id}
              seen={seen}
              enrolled={enrolled}
              onChanged={onPeopleChanged}
            />
          ))}
        </ul>
      )}
      {expanded && (
        <div className="event-detail">
          {event.clip === null ? (
            <p className="muted">no clip for this event</p>
          ) : clipFailed ? (
            <p className="muted">clip not available right now; the upstream NVR may be unreachable</p>
          ) : (
            <video
              className="event-clip"
              controls
              preload="none"
              src={event.clip}
              onError={() => setClipFailed(true)}
            />
          )}
          <OtherCameras eventId={event.id} />
        </div>
      )}
    </li>
  );
}

function clockOf(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/**
 * The other cameras at the event's moment (ADR-0017): every clip around it, the same
 * person only when a face says so, and all of them at once as one mosaic video.
 * Loaded when the card opens; nothing is stored.
 */
function OtherCameras({ eventId }: { eventId: string }) {
  const [linked, setLinked] = useState<Linked | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playing, setPlaying] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    api
      .eventLinked(eventId)
      .then((found) => alive && setLinked(found))
      .catch((err: unknown) => alive && setError(err instanceof Error ? err.message : String(err)));
    return () => {
      alive = false;
    };
  }, [eventId]);
  if (error !== null) return <p className="muted">other cameras: {error}</p>;
  if (linked === null) return <p className="muted">looking at the other cameras…</p>;
  const others = linked.clips.filter((clip) => !clip.own);
  return (
    <section className="linked" aria-label="Other cameras at this moment">
      <h4 className="linked-title">
        Other cameras, {clockOf(linked.start)}–{clockOf(linked.end)}
      </h4>
      {others.length === 0 ? (
        <p className="muted">
          no other camera recorded anything then (battery cameras record only while awake)
        </p>
      ) : (
        <ul className="linked-list">
          {others.map((clip) => {
            const key = `${clip.camera}-${clip.started_at}`;
            return (
              <li key={key} className="linked-item">
                <button
                  type="button"
                  className="linked-play"
                  disabled={clip.clip === null}
                  onClick={() => setPlaying(playing === key ? null : key)}
                >
                  {clip.camera} {clip.approximate ? "≈ " : ""}
                  {clockOf(clip.started_at)}–{clockOf(clip.ended_at)}
                </button>
                {clip.same_person ? (
                  <span className="linked-tag">same person (face)</span>
                ) : clip.persons ? (
                  <span className="linked-tag muted">a person, face not compared</span>
                ) : null}
                {clip.approximate && (
                  <span className="linked-tag muted" title="this camera's video can arrive late">
                    time approximate
                  </span>
                )}
                {playing === key && clip.clip !== null && (
                  <video className="event-clip" controls autoPlay playsInline src={clip.clip} />
                )}
              </li>
            );
          })}
        </ul>
      )}
      {linked.silent_cameras.length > 0 && (
        <p className="muted">nothing recorded then: {linked.silent_cameras.join(", ")}</p>
      )}
      {linked.mosaic !== null && (
        <details className="linked-mosaic">
          <summary>All cameras at once (one video)</summary>
          <video className="event-clip" controls preload="none" playsInline src={linked.mosaic} />
        </details>
      )}
    </section>
  );
}

/**
 * Events feed: incidents from Frigate (and, from week 2, their verdicts), newest first, grouped by local
 * day. Live over the server's WebSocket — confirmed/ended events nudge a
 * coalesced refetch — with the 10 s poll kept as a fallback that stands down
 * while the socket is open (and pauses while the tab is hidden). Refreshes keep
 * stable card keys so the layout never jumps under the cursor.
 */
export function EventsPage() {
  // Cameras seen in any loaded feed: they come from Frigate, so the feed is the source.
  const [cameras, setCameras] = useState<string[]>([]);
  const [cameraFilter, setCameraFilter] = useState("");
  const [favoriteOnly, setFavoriteOnly] = useState(false);
  const [events, setEvents] = useState<EventInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [votes, setVotes] = useState<Record<string, "up" | "down">>({});
  const [voteError, setVoteError] = useState<string | null>(null);
  const [favoriteError, setFavoriteError] = useState<string | null>(null);
  const [wsLive, setWsLive] = useState(false);
  // Enrolled people, for the face answers; stays empty when known people are off or the
  // session may not see them (viewers).
  const [enrolled, setEnrolled] = useState<Enrolled[]>([]);
  useEffect(() => {
    api
      .people()
      .then((state) =>
        setEnrolled(state.enabled ? state.people.map((p) => ({ id: p.id, name: p.name })) : []),
      )
      .catch(() => setEnrolled([]));
  }, []);
  const setEventPeople = useCallback((id: string, people: EventPeople) => {
    setEvents((current) =>
      current === null
        ? current
        : current.map((event) => (event.id === id ? { ...event, people } : event)),
    );
  }, []);
  // Mirrors `wsLive` for the poll tick, which must not re-arm the interval.
  const wsLiveRef = useRef(false);
  // Latest feed refresh, so the (mount-once) socket always refetches with the
  // current filters. Reset to a no-op on cleanup.
  const refreshRef = useRef<() => void>(() => {});

  // Remember every camera the feed has shown, so filtering never empties the select.
  useEffect(() => {
    if (events === null) return;
    setCameras((known) => {
      const merged = new Set(known);
      for (const event of events) merged.add(event.camera);
      return merged.size === known.length ? known : [...merged].sort();
    });
  }, [events]);

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
        if (topic === "event.observed" || topic === "event.confirmed" || topic === "event.ended") {
          scheduleRefetch();
        }
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
            {cameras.map((cam) => (
              <option key={cam} value={cam}>
                {cam}
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
                Events appear when Frigate reports review items. Check the <code>frigate:</code>
                section of your config and the System page
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
                enrolled={enrolled}
                onPeopleChanged={(people) => setEventPeople(event.id, people)}
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
