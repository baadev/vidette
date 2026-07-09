// Typed API client for the Vidette server (see docs/api.md).
//
// Conventions:
// - Every request is sent with `credentials: "same-origin"` — the session cookie is
//   httpOnly, so this module never sees or stores tokens.
// - Non-2xx responses throw `ApiError` whose message is the `detail` string from the
//   server's problem-json body when present (the server always makes it actionable).
// - A 401 from any endpoint except login/bootstrap/me dispatches `UNAUTHORIZED_EVENT`
//   on `window`, so the app shell can fall back to the login screen when a session
//   expires mid-use. `me()` maps 401 to `null` and never throws for it.

export type Camera = {
  id: string;
  name: string;
  adapter: string;
  record_mode: string;
  state: string;
  last_segment_at: number | null;
  stream_ready: boolean;
};

export type SegmentInfo = {
  id: number;
  start_ts: number;
  end_ts: number;
  size_bytes: number;
};

export type HourBucket = {
  hour_start_ts: number;
  recorded_seconds: number;
  bytes: number;
};

export type ExportJob = {
  id: string;
  state: "queued" | "running" | "done" | "error";
  error: string | null;
};

/** Tier-2 trajectory geometry attached to an event (pure math, no model opinions). */
export type EventGeometry = {
  approach: number | null;
  dwell_s: number | null;
  touch: boolean;
  loiter: boolean;
  repeat_pass: number;
};

/**
 * One understood event from the cascade (M2+). `snapshot` and `clip` are direct
 * media URL paths — they can 404 until the underlying footage lands on disk.
 */
export type EventInfo = {
  id: string;
  camera: string;
  started_at: number;
  ended_at: number | null;
  /** Lifecycle state, e.g. "confirmed" or "dismissed" (more states may appear). */
  state: string;
  kinds: string[];
  zones: string[];
  geometry: EventGeometry;
  summary: string | null;
  policy: string | null;
  feedback: "up" | "down" | null;
  snapshot: string | null;
  clip: string;
};

export type AuthStatus = {
  bootstrapped: boolean;
  mode: string;
};

export type Me = {
  username: string;
  role: string;
};

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** Dispatched on `window` when any authenticated request comes back 401. */
export const UNAUTHORIZED_EVENT = "vidette:unauthorized";

const BASE = "/api/v1";

/** How a 401 response should be handled before the ApiError is thrown. */
type On401 = "signal" | "silent";

function extractDetail(body: unknown): string | null {
  if (typeof body !== "object" || body === null) return null;
  // FastAPI wraps HTTPException payloads as {"detail": ...}; our servers put a
  // problem-json-shaped object there ({"type", "title", "detail"}), but `detail`
  // can also be a plain string (or a validation-error list, which we skip).
  const outer = (body as { detail?: unknown }).detail;
  const candidates: unknown[] = [outer, body];
  for (const candidate of candidates) {
    if (typeof candidate === "string" && candidate.length > 0) return candidate;
    if (typeof candidate === "object" && candidate !== null && !Array.isArray(candidate)) {
      const problem = candidate as { detail?: unknown; title?: unknown };
      if (typeof problem.detail === "string" && problem.detail.length > 0) return problem.detail;
      if (typeof problem.title === "string" && problem.title.length > 0) return problem.title;
    }
  }
  return null;
}

async function errorFrom(response: Response): Promise<ApiError> {
  let detail: string | null = null;
  try {
    detail = extractDetail(await response.json());
  } catch {
    detail = null; // non-JSON body — fall through to the generic message
  }
  const fallback = `request failed with HTTP ${response.status}${
    response.statusText ? ` ${response.statusText}` : ""
  }`;
  return new ApiError(response.status, detail ?? fallback);
}

async function send(path: string, init: RequestInit, on401: On401 = "signal"): Promise<Response> {
  const response = await fetch(path, { credentials: "same-origin", ...init });
  if (response.status === 401 && on401 === "signal") {
    window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  }
  if (!response.ok) throw await errorFrom(response);
  return response;
}

async function getJson<T>(path: string, on401: On401 = "signal"): Promise<T> {
  const response = await send(path, { headers: { accept: "application/json" } }, on401);
  return (await response.json()) as T;
}

async function postJson<T>(path: string, body: unknown, on401: On401 = "signal"): Promise<T> {
  const response = await send(
    path,
    {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify(body),
    },
    on401,
  );
  return (await response.json()) as T;
}

export const api = {
  authStatus(): Promise<AuthStatus> {
    return getJson<AuthStatus>(`${BASE}/auth/status`);
  },

  bootstrap(username: string, password: string): Promise<Me> {
    // 400/409 here are part of the wizard flow — never a session-expiry signal.
    return postJson<Me>(`${BASE}/auth/bootstrap`, { username, password }, "silent");
  },

  login(username: string, password: string): Promise<Me> {
    // A 401 here means "wrong credentials", not "session expired" — keep it local.
    return postJson<Me>(`${BASE}/auth/login`, { username, password }, "silent");
  },

  async logout(): Promise<void> {
    await send(`${BASE}/auth/logout`, { method: "POST" }, "silent"); // 204, no body
  },

  async me(): Promise<Me | null> {
    const response = await fetch(`${BASE}/auth/me`, {
      credentials: "same-origin",
      headers: { accept: "application/json" },
    });
    if (response.status === 401) return null;
    if (!response.ok) throw await errorFrom(response);
    return (await response.json()) as Me;
  },

  cameras(): Promise<Camera[]> {
    return getJson<Camera[]>(`${BASE}/cameras`);
  },

  recordings(camera: string, fromTs: number, toTs: number): Promise<SegmentInfo[]> {
    const query = new URLSearchParams({
      camera,
      from: String(fromTs),
      to: String(toTs),
    });
    return getJson<SegmentInfo[]>(`${BASE}/recordings?${query}`);
  },

  summary(camera: string, day: string): Promise<HourBucket[]> {
    const query = new URLSearchParams({ camera, day });
    return getJson<HourBucket[]>(`${BASE}/recordings/summary?${query}`);
  },

  createExport(camera: string, fromTs: number, toTs: number): Promise<ExportJob> {
    return postJson<ExportJob>(`${BASE}/export`, { camera, from: fromTs, to: toTs });
  },

  exportStatus(id: string): Promise<ExportJob> {
    return getJson<ExportJob>(`${BASE}/export/${encodeURIComponent(id)}`);
  },

  events(opts?: { camera?: string; sinceTs?: number; limit?: number }): Promise<EventInfo[]> {
    const query = new URLSearchParams();
    if (opts?.camera) query.set("camera", opts.camera);
    if (opts?.sinceTs !== undefined) query.set("since_ts", String(opts.sinceTs));
    if (opts?.limit !== undefined) query.set("limit", String(opts.limit));
    const qs = query.toString();
    return getJson<EventInfo[]>(qs ? `${BASE}/events?${qs}` : `${BASE}/events`);
  },

  async eventFeedback(id: string, verdict: "up" | "down"): Promise<void> {
    // 204, no body.
    await send(`${BASE}/events/${encodeURIComponent(id)}/feedback`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ verdict }),
    });
  },

  async whep(camera: string, offerSdp: string): Promise<string> {
    const response = await send(`${BASE}/streams/${encodeURIComponent(camera)}/whep`, {
      method: "POST",
      headers: { "content-type": "application/sdp" },
      body: offerSdp,
    });
    return await response.text();
  },
};

export const segmentFileUrl = (id: number): string => `${BASE}/recordings/segments/${id}/file`;

export const snapshotUrl = (camera: string): string =>
  `${BASE}/streams/${encodeURIComponent(camera)}/snapshot.jpeg`;

export const exportDownloadUrl = (id: string): string =>
  `${BASE}/export/${encodeURIComponent(id)}/download`;

/** Direct media URL of an hour's preview MP4 — 404 until it has been generated. */
export const previewUrl = (camera: string, hourStartTs: number): string =>
  `${BASE}/recordings/preview?camera=${encodeURIComponent(camera)}&hour_start_ts=${hourStartTs}`;
