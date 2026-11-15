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

/** Tier-2 trajectory geometry attached to an event (pure math, no model opinions). */
export type EventGeometry = {
  approach: number | null;
  dwell_s: number | null;
  touch: boolean;
  loiter: boolean;
  repeat_pass: number;
};

/**
 * One event (since v0.2: an incident from Frigate). `snapshot` and `clip` are
 * direct media URL paths, or null when there is none.
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
  /** Starred by the user (see `api.setEventFavorite`). */
  favorite: boolean;
  snapshot: string | null;
  clip: string | null;
  /** Where the event came from: "frigate" since v0.2; null for v0.1 rows. */
  source?: string | null;
  /** What the upstream NVR said: its triage and, with Frigate 0.17+, its own summary. */
  upstream?: UpstreamFacts | null;
  /** The local face check (known people), when it is on; null when not checked. */
  people?: EventPeople | null;
};

/** One person seen in an event: faces grouped across frames. */
export type SeenPerson = {
  n: number;
  /** "unclear": a face followed through frames but never clear enough to compare. */
  status: "known" | "unknown" | "unclear";
  person: string | null;
  name: string | null;
  score: number;
  faces: number;
  face_px: number;
  /** URL of the best face crop, or null. */
  crop: string | null;
  /** A person id, "stranger" or "unsure" once someone answered. */
  confirmed: string | null;
  /** The anonymous identity of an unknown face (people.track_unknown, ADR-0014). */
  identity?: number | null;
  /** How often it had been seen when this event was checked. */
  identity_note?: string | null;
  identity_name?: string | null;
  identity_status?: IdentityStatus | null;
  /** An unknown face's nearest enrolled person (a suggestion only). */
  closest?: string | null;
  /** Under 64 px or unclear: it may not be recognized next time. */
  small?: boolean;
};

export type EventPeople = {
  summary: string;
  frames_checked: number;
  unclear_frames: number;
  seen: SeenPerson[];
  error: string | null;
};

export type PersonFace = { id: string; kind: "enrolled" | "confirmed"; url: string };

export type Person = {
  id: string;
  name: string;
  created_at: string;
  enrolled: number;
  confirmed: number;
  photos: number;
  threshold: number;
  rejected: number;
  faces: PersonFace[];
};

export type PeopleState = {
  enabled: boolean;
  /** Faces can be found here: OpenCV and the models are loaded. */
  available: boolean;
  /** Why not, and what to do. */
  reason: string | null;
  dir: string | null;
  threshold: number;
  people: Person[];
  not_enrolled: string[];
  warnings: string[];
};

export type UploadResult = {
  faces_found: number;
  readable: number;
  added: number;
  duplicates: number;
  unclear: number;
  skipped: string[];
  person: Person;
};

export type ConfirmResult = { message: string; people: EventPeople };

export type IdentityStatus = "unknown" | "suspicious" | "familiar" | "known";

/** An anonymous identity: a face nobody enrolled, seen again (docs/people.md, ADR-0014). */
export type Identity = {
  id: number;
  /** "Stranger #12", or the name someone gave it. */
  label: string;
  name: string | null;
  status: IdentityStatus;
  /** The enrolled person it became, once promoted. */
  person: string | null;
  sightings: number;
  days: number;
  first_seen: number | null;
  last_seen: number | null;
  cameras: string[];
  /** URL of the best face crop. */
  face: string | null;
  /** Only unclear faces: joined by hand only. */
  weak?: boolean;
  /** Came close to someone enrolled: probably them. */
  near_known?: boolean;
};

export type Duplicate = { a: number; b: number; score: number; weak: boolean };

export type IdentitiesState = {
  enabled: boolean;
  reason: string | null;
  dir: string | null;
  retention_days: number;
  match: number;
  identities: Identity[];
  duplicates: Duplicate[];
  warnings: string[];
};

export type Sighting = {
  id: number;
  at: number;
  camera: string;
  faces: number;
  face_px: number;
  face: string | null;
  /** URL of the clip, when it is still there. */
  clip: string | null;
  /** The event this sighting belongs to, when there is one. */
  event: string | null;
  manual?: boolean;
  weak?: boolean;
  near_known?: boolean;
};

export type IdentityDetail = Identity & {
  sightings_list: Sighting[];
  similar: { id: number; label: string; score: number; face: string | null; weak?: boolean }[];
};

export type UpstreamFacts = {
  severity: string | null;
  detections: string[];
  sub_labels: string[];
  summary: {
    title: string | null;
    text: string | null;
    threat_level: number | null;
    confidence: number | null;
    concerns: string[];
  } | null;
};

export type AuthStatus = {
  bootstrapped: boolean;
  mode: string;
};

export type Me = {
  username: string;
  role: string;
};

/** Which VLM judges incidents (docs/vlm.md). */
export type VlmBackend = "none" | "local" | "cloud";

export type VlmOption = {
  id: VlmBackend;
  label: string;
  available: boolean;
  /** Why it cannot be chosen (e.g. no cloud section in the config). */
  reason: string | null;
  model: string | null;
  endpoint: string | null;
  /** One honest line: where the frames go. */
  privacy: string;
  cloud: boolean;
  usage_today: { calls: number; limit: number; usd: number } | null;
};

export type VlmSettings = {
  backend: VlmBackend;
  /** "ui": chosen on this page and stored; "config": understanding.vlm.backend. */
  source: "ui" | "config";
  config_backend: VlmBackend;
  frames_per_call: number;
  options: VlmOption[];
  worker: {
    running: boolean;
    policies: number;
    judged: number;
    confirmed: number;
    dismissed: number;
    undecided: number;
    note: string;
  };
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

async function putJson<T>(path: string, body: unknown): Promise<T> {
  const response = await send(path, {
    method: "PUT",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(body),
  });
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

  events(opts?: {
    camera?: string;
    sinceTs?: number;
    limit?: number;
    favoriteOnly?: boolean;
  }): Promise<EventInfo[]> {
    const query = new URLSearchParams();
    if (opts?.camera) query.set("camera", opts.camera);
    if (opts?.sinceTs !== undefined) query.set("since_ts", String(opts.sinceTs));
    if (opts?.limit !== undefined) query.set("limit", String(opts.limit));
    if (opts?.favoriteOnly) query.set("favorite", "true");
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

  /** Public VAPID key for Web Push subscriptions. */
  vapidKey(): Promise<{ key: string }> {
    return getJson<{ key: string }>(`${BASE}/push/vapid-key`);
  },

  async pushSubscribe(subscription: unknown): Promise<void> {
    // 204, no body.
    await send(`${BASE}/push/subscriptions`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(subscription),
    });
  },

  async pushUnsubscribe(endpoint: string): Promise<void> {
    // 204, no body.
    await send(`${BASE}/push/subscriptions`, {
      method: "DELETE",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ endpoint }),
    });
  },

  vlmSettings(): Promise<VlmSettings> {
    return getJson<VlmSettings>(`${BASE}/settings/vlm`);
  },

  /** Store the choice server-side; it wins over the config until changed again. */
  setVlmBackend(backend: VlmBackend): Promise<VlmSettings> {
    return putJson<VlmSettings>(`${BASE}/settings/vlm`, { backend });
  },

  /** Known people (docs/people.md): read:config to look, write:config to change. */
  people(): Promise<PeopleState> {
    return getJson<PeopleState>(`${BASE}/people`);
  },

  createPerson(name: string): Promise<Person> {
    return postJson<Person>(`${BASE}/people`, { name });
  },

  async deletePerson(id: string): Promise<void> {
    await send(`${BASE}/people/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  /** One photo as the raw request body; every readable face in it is enrolled. */
  async uploadPhoto(id: string, file: File): Promise<UploadResult> {
    const response = await send(`${BASE}/people/${encodeURIComponent(id)}/photos`, {
      method: "POST",
      headers: { "content-type": file.type || "image/jpeg", accept: "application/json" },
      body: file,
    });
    return (await response.json()) as UploadResult;
  },

  async deleteFace(personId: string, faceId: string): Promise<void> {
    await send(
      `${BASE}/people/${encodeURIComponent(personId)}/faces/${encodeURIComponent(faceId)}`,
      { method: "DELETE" },
    );
  },

  /** "This is Sam" / "a stranger" / "not sure" for face `n` of an event. */
  confirmEventFace(eventId: string, n: number, answer: string): Promise<ConfirmResult> {
    return postJson<ConfirmResult>(
      `${BASE}/events/${encodeURIComponent(eventId)}/people/${n}`,
      { answer },
    );
  },

  /** Anonymous identities (ADR-0014): read:config to look, write:config to change. */
  identities(): Promise<IdentitiesState> {
    return getJson<IdentitiesState>(`${BASE}/identities`);
  },

  identity(id: number): Promise<IdentityDetail> {
    return getJson<IdentityDetail>(`${BASE}/identities/${id}`);
  },

  async changeIdentity(
    id: number,
    change: { name?: string; clear_name?: boolean; status?: "unknown" | "suspicious" | "familiar" },
  ): Promise<IdentityDetail> {
    const response = await send(`${BASE}/identities/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify(change),
    });
    return (await response.json()) as IdentityDetail;
  },

  mergeIdentity(id: number, into: number): Promise<IdentityDetail> {
    return postJson<IdentityDetail>(`${BASE}/identities/${id}/merge`, { into });
  },

  splitIdentity(id: number, sightings: number[]): Promise<IdentityDetail> {
    return postJson<IdentityDetail>(`${BASE}/identities/${id}/split`, { sightings });
  },

  promoteIdentity(
    id: number,
    person: string,
  ): Promise<{ identity: Identity; person: string; faces_added: number }> {
    return postJson(`${BASE}/identities/${id}/promote`, { person });
  },

  mergeIdentities(identities: number[], into: number): Promise<IdentityDetail> {
    return postJson<IdentityDetail>(`${BASE}/identities/merge`, { identities, into });
  },

  /** One sighting to another identity, a new one (`into` null), or a known person. */
  moveSighting(
    sighting: number,
    target: { into?: number | null; name?: string; status?: "unknown" | "suspicious" | "familiar"; person?: string },
  ): Promise<IdentityDetail> {
    return postJson<IdentityDetail>(`${BASE}/identities/sightings/${sighting}/move`, target);
  },

  demoteIdentity(id: number): Promise<IdentityDetail> {
    return postJson<IdentityDetail>(`${BASE}/identities/${id}/demote`, {});
  },

  /** Put face `n` of an event in an identity, or in a new one with a name and status. */
  faceIdentity(
    eventId: string,
    n: number,
    choice: { identity?: number; name?: string; status?: "unknown" | "familiar" | "suspicious" },
  ): Promise<ConfirmResult> {
    return postJson<ConfirmResult>(
      `${BASE}/events/${encodeURIComponent(eventId)}/people/${n}/identity`,
      choice,
    );
  },

  /** Take the decision about face `n` of an event back. */
  async undoFace(eventId: string, n: number): Promise<ConfirmResult> {
    const response = await send(`${BASE}/events/${encodeURIComponent(eventId)}/people/${n}`, {
      method: "DELETE",
      headers: { accept: "application/json" },
    });
    return (await response.json()) as ConfirmResult;
  },

  async deleteIdentity(id: number): Promise<void> {
    await send(`${BASE}/identities/${id}`, { method: "DELETE" });
  },

  async setEventFavorite(id: string, favorite: boolean): Promise<void> {
    // 204, no body.
    await send(`${BASE}/events/${encodeURIComponent(id)}/favorite`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ favorite }),
    });
  },
};

/** WebSocket endpoint for live server events (same-origin, ws/wss follows the page). */
export const wsUrl = (): string =>
  `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/v1/ws`;
