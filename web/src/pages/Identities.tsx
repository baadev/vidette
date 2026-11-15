import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiError,
  api,
  type Duplicate,
  type IdentitiesState,
  type Identity,
  type IdentityDetail,
  type IdentityStatus,
  type Sighting,
} from "../api";
import "./pages.css";

// Anonymous identities (docs/people.md, ADR-0014, ADR-0016): every person whose face was
// found and who is not enrolled. Nobody is named here unless someone in the household types
// a name; counts and cameras are all the system knows. Everything is matched and stored on
// the server's own machine. Every action here can be taken back: a merge by moving
// sightings out again, a promotion with "Not them", a status by choosing another.

const STATUS_LABEL: Record<IdentityStatus, string> = {
  unknown: "Unknown",
  suspicious: "Suspicious",
  familiar: "Familiar",
  known: "Known",
};

function describeError(err: unknown, doing: string): string {
  const detail = err instanceof Error ? err.message : String(err);
  return `Could not ${doing}: ${detail}`;
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** "today 21:03", "yesterday 08:15", "12 Nov 17:40". */
export function when(ts: number | null): string {
  if (ts === null) return "never";
  const at = new Date(ts * 1000);
  const clock = `${pad2(at.getHours())}:${pad2(at.getMinutes())}`;
  const today = new Date();
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
  if (at.toDateString() === today.toDateString()) return `today ${clock}`;
  if (at.toDateString() === yesterday.toDateString()) return `yesterday ${clock}`;
  const day = at.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  return `${day} ${clock}`;
}

function plural(count: number, word: string): string {
  if (count === 1) return `${count} ${word}`;
  return `${count} ${word.endsWith("y") ? `${word.slice(0, -1)}ies` : `${word}s`}`;
}

function StatusBadge({ status }: { status: IdentityStatus }) {
  if (status === "unknown") return null;
  return <span className={`id-status id-status-${status}`}>{STATUS_LABEL[status]}</span>;
}

function Face({ src, size = "small" }: { src: string | null; size?: "small" | "large" }) {
  return src ? (
    <img className={`id-face id-face-${size}`} src={src} alt="" loading="lazy" />
  ) : (
    <span className={`id-face id-face-${size} id-face-empty`} aria-hidden="true" />
  );
}

/** Known people and identities are two views of one tab: "who the camera saw". */
export function PeopleSwitch({ current }: { current: "people" | "identities" }) {
  return (
    <nav className="people-switch" aria-label="People views">
      <a href="#/people" className={current === "people" ? "active" : ""}>
        Known people
      </a>
      <a href="#/identities" className={current === "identities" ? "active" : ""}>
        Identities
      </a>
    </nav>
  );
}

// --- the list ----------------------------------------------------------------------------

type Filter = "all" | "small" | IdentityStatus;

function IdentityItem({
  identity,
  selecting,
  selected,
  onToggle,
}: {
  identity: Identity;
  selecting: boolean;
  selected: boolean;
  onToggle: () => void;
}) {
  const body = (
    <>
      {selecting && (
        <input
          type="checkbox"
          className="id-check"
          checked={selected}
          onChange={onToggle}
          aria-label={`Select ${identity.label}`}
        />
      )}
      <Face src={identity.face} />
      <span className="id-item-main">
        <span className="id-item-title">
          <span className="id-label">{identity.label}</span>
          <StatusBadge status={identity.status} />
          {identity.weak && <span className="id-status">small face</span>}
        </span>
        <span className="muted id-item-stats">
          {when(identity.last_seen)} · {identity.cameras.join(", ")}
          {identity.days > 1 ? ` · ${plural(identity.days, "day")}` : ""}
        </span>
      </span>
      <span className="id-count" aria-label={`${identity.sightings} sightings`}>
        ×{identity.sightings}
      </span>
    </>
  );
  return (
    <li>
      {selecting ? (
        <label className={`card id-item${selected ? " id-item-selected" : ""}`}>{body}</label>
      ) : (
        <a className="card id-item" href={`#/identities/${identity.id}`}>
          {body}
        </a>
      )}
    </li>
  );
}

function DuplicatePair({
  pair,
  byId,
  busy,
  onMerge,
}: {
  pair: Duplicate;
  byId: Map<number, Identity>;
  busy: boolean;
  onMerge: (from: number, into: number) => void;
}) {
  const a = byId.get(pair.a);
  const b = byId.get(pair.b);
  if (!a || !b) return null;
  // the one seen more often (or the older) stays; the other moves into it
  const [keep, move] = a.sightings > b.sightings || (a.sightings === b.sightings && a.id < b.id) ? [a, b] : [b, a];
  return (
    <li className="card id-pair">
      <a href={`#/identities/${keep.id}`} className="id-pair-face">
        <Face src={keep.face} />
        <span className="id-item-stats">{keep.label}</span>
      </a>
      <a href={`#/identities/${move.id}`} className="id-pair-face">
        <Face src={move.face} />
        <span className="id-item-stats">{move.label}</span>
      </a>
      <span className="id-pair-actions">
        <span className="muted id-item-stats">
          {pair.score.toFixed(2)}
          {pair.weak ? " · small faces" : ""}
        </span>
        <button type="button" className="ghost" disabled={busy} onClick={() => onMerge(move.id, keep.id)}>
          Same person
        </button>
      </span>
    </li>
  );
}

function IdentityList() {
  const [state, setState] = useState<IdentitiesState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [order, setOrder] = useState<"recent" | "most">("recent");
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());

  const load = useCallback(() => {
    api
      .identities()
      .then((next) => {
        setState(next);
        setError(null);
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.status === 403) {
          setError("Only an admin can see identities: they are faces.");
        } else {
          setError(describeError(err, "load identities"));
        }
      });
  }, []);

  useEffect(load, [load]);

  const counts = useMemo(() => {
    const found: Record<IdentityStatus, number> = { unknown: 0, suspicious: 0, familiar: 0, known: 0 };
    for (const identity of state?.identities ?? []) found[identity.status] += 1;
    return found;
  }, [state]);
  const byId = useMemo(() => new Map((state?.identities ?? []).map((i) => [i.id, i])), [state]);

  const merge = async (ids: number[], into: number) => {
    setBusy(true);
    setNote(null);
    try {
      await api.mergeIdentities(ids, into);
      setNote(`${plural(ids.length, "identity")} merged into #${into}.`);
      setSelected(new Set());
      setSelecting(false);
      load();
    } catch (err) {
      setError(describeError(err, "merge"));
    } finally {
      setBusy(false);
    }
  };

  const small = (state?.identities ?? []).filter((identity) => identity.weak).length;
  const shown = (state?.identities ?? [])
    .filter((identity) =>
      filter === "all" ? true : filter === "small" ? identity.weak : identity.status === filter,
    )
    .sort((a, b) =>
      order === "most"
        ? b.sightings - a.sightings || (b.last_seen ?? 0) - (a.last_seen ?? 0)
        : (b.last_seen ?? 0) - (a.last_seen ?? 0),
    );
  const recurring = (state?.identities ?? []).filter((identity) => identity.sightings > 1).length;
  const pairs = (state?.duplicates ?? []).filter((pair) => !dismissed.has(`${pair.a}-${pair.b}`));
  const chosen = [...selected];
  const target = chosen.length
    ? chosen.reduce((best, id) => ((byId.get(id)?.sightings ?? 0) > (byId.get(best)?.sightings ?? 0) ? id : best))
    : null;

  return (
    <main className="page identities-page">
      <header className="page-header">
        <h1 className="page-title">Identities</h1>
        <PeopleSwitch current="identities" />
      </header>
      <p className="kbd-hint">
        Everyone whose face was found and who is not enrolled. No names unless you give one; the
        judge hears only how often and where.
      </p>

      {error && <p className="page-error">{error}</p>}
      {note && <p className="person-note">{note}</p>}
      {!error && state === null && <p className="page-loading">Loading…</p>}

      {state !== null && !state.enabled && (
        <div className="empty-state">
          <p>Identities are off.</p>
          <p>{state.reason}</p>
          <p>
            They keep face crops and embeddings of passers-by, who never agreed to it: turn them on
            only where that is allowed, and only on this machine.
          </p>
        </div>
      )}

      {state !== null && state.enabled && (
        <>
          {state.warnings.map((warning) => (
            <p key={warning} className="page-error">
              {warning}
            </p>
          ))}
          <p className="muted id-summary">
            {plural(state.identities.length, "identity")}, {recurring} seen more than once.
          </p>

          {pairs.length > 0 && !selecting && (
            <section className="id-section">
              <h2 className="id-section-title">Maybe the same person</h2>
              <p className="muted id-item-stats">
                Faces alike enough to ask, not enough to merge on their own. Merge only when you are
                sure: two people in one identity make visits that never happened.
              </p>
              <ul className="id-pairs">
                {pairs.slice(0, 6).map((pair) => (
                  <DuplicatePair
                    key={`${pair.a}-${pair.b}`}
                    pair={pair}
                    byId={byId}
                    busy={busy}
                    onMerge={(from, into) => void merge([from], into)}
                  />
                ))}
              </ul>
              {pairs.length > 0 && (
                <button
                  type="button"
                  className="ghost"
                  onClick={() =>
                    setDismissed(new Set([...dismissed, ...pairs.slice(0, 6).map((p) => `${p.a}-${p.b}`)]))
                  }
                >
                  Not the same: hide these
                </button>
              )}
            </section>
          )}

          <div className="id-filters" role="group" aria-label="Show">
            {(["all", "suspicious", "unknown", "familiar", "known"] as const)
              .filter((key) => key === "all" || counts[key] > 0)
              .map((key) => (
                <button
                  key={key}
                  type="button"
                  className={`ghost${filter === key ? " id-filter-active" : ""}`}
                  aria-pressed={filter === key}
                  onClick={() => setFilter(key)}
                >
                  {key === "all" ? "All" : STATUS_LABEL[key]}{" "}
                  {key === "all" ? state.identities.length : counts[key]}
                </button>
              ))}
            {small > 0 && (
              <button
                type="button"
                className={`ghost${filter === "small" ? " id-filter-active" : ""}`}
                aria-pressed={filter === "small"}
                onClick={() => setFilter("small")}
              >
                Small faces {small}
              </button>
            )}
            <button
              type="button"
              className="ghost"
              onClick={() => setOrder((current) => (current === "recent" ? "most" : "recent"))}
            >
              {order === "recent" ? "Recent first" : "Most seen first"}
            </button>
            <button
              type="button"
              className="ghost id-order"
              onClick={() => {
                setSelecting((on) => !on);
                setSelected(new Set());
              }}
            >
              {selecting ? "Cancel" : "Select…"}
            </button>
          </div>
          {selecting && (
            <div className="id-split-bar">
              <span className="muted id-item-stats">
                {selected.size < 2
                  ? "Select the identities that are one person."
                  : `Into ${byId.get(target ?? 0)?.label ?? ""}, the one seen most.`}
              </span>
              <button
                type="button"
                className="primary"
                disabled={busy || selected.size < 2 || target === null}
                onClick={() => target !== null && void merge(chosen.filter((id) => id !== target), target)}
              >
                Merge {selected.size || ""}
              </button>
            </div>
          )}
          {shown.length === 0 ? (
            <div className="empty-state">
              <p>
                {state.identities.length === 0
                  ? "Nobody yet. Everyone whose face is found gets an identity."
                  : "None with this status."}
              </p>
            </div>
          ) : (
            <ul className="id-list">
              {shown.map((identity) => (
                <IdentityItem
                  key={identity.id}
                  identity={identity}
                  selecting={selecting}
                  selected={selected.has(identity.id)}
                  onToggle={() =>
                    setSelected((current) => {
                      const next = new Set(current);
                      if (next.has(identity.id)) next.delete(identity.id);
                      else next.add(identity.id);
                      return next;
                    })
                  }
                />
              ))}
            </ul>
          )}
          <p className="muted people-footnote">
            Kept in {state.dir ?? "the identities folder"} on this machine, readable by its owner
            only. An identity nobody named or flagged is deleted {state.retention_days} days after it
            was last seen; deleting one removes every crop and embedding it holds.
          </p>
        </>
      )}
    </main>
  );
}

// --- one identity ----------------------------------------------------------------------------

function SightingRow({
  sighting,
  selecting,
  selected,
  others,
  people,
  busy,
  onToggle,
  onMove,
}: {
  sighting: Sighting;
  selecting: boolean;
  selected: boolean;
  others: Identity[];
  people: string[];
  busy: boolean;
  onToggle: () => void;
  onMove: (target: { into?: number | null; person?: string }) => void;
}) {
  const [playing, setPlaying] = useState(false);
  return (
    <li className={`id-sighting${selected ? " id-sighting-selected" : ""}`}>
      <div className="id-sighting-row">
        {selecting && (
          <input
            type="checkbox"
            className="id-check"
            checked={selected}
            onChange={onToggle}
            aria-label={`Select the sighting of ${when(sighting.at)}`}
          />
        )}
        <Face src={sighting.face} />
        <div className="id-sighting-info">
          <span>
            {when(sighting.at)} · <span className="id-camera">{sighting.camera}</span>
          </span>
          <span className="muted id-item-stats">
            face {sighting.face_px}px · {plural(sighting.faces, "frame")}
            {sighting.weak ? " · small" : ""}
            {sighting.manual ? " · added by hand" : ""}
            {sighting.near_known ? " · close to someone enrolled" : ""}
          </span>
        </div>
        {sighting.clip && !selecting && (
          <button type="button" className="ghost" onClick={() => setPlaying((on) => !on)}>
            {playing ? "Hide" : "Play"}
          </button>
        )}
      </div>
      {!selecting && (
        <select
          className="face-pick"
          aria-label="Move this sighting"
          disabled={busy}
          value=""
          onChange={(event) => {
            const value = event.target.value;
            if (value === "new") onMove({ into: null });
            else if (value.startsWith("person:")) onMove({ person: value.slice(7) });
            else if (value) onMove({ into: Number(value) });
          }}
        >
          <option value="">Not this person? Move to…</option>
          <option value="new">A new identity</option>
          {people.map((name) => (
            <option key={`p-${name}`} value={`person:${name}`}>
              Known: {name}
            </option>
          ))}
          {others.map((identity) => (
            <option key={identity.id} value={identity.id}>
              {identity.label} · ×{identity.sightings}
            </option>
          ))}
        </select>
      )}
      {playing && sighting.clip && (
        <video className="id-video" src={sighting.clip} controls autoPlay playsInline preload="none" />
      )}
    </li>
  );
}

function IdentityView({ id }: { id: number }) {
  const [detail, setDetail] = useState<IdentityDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState("");
  const [person, setPerson] = useState("");
  const [enrolled, setEnrolled] = useState<string[]>([]);
  const [others, setOthers] = useState<Identity[]>([]);
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());

  const show = useCallback((next: IdentityDetail) => {
    setDetail(next);
    setName(next.name ?? "");
    setError(null);
  }, []);

  useEffect(() => {
    setDetail(null);
    setSelecting(false);
    setSelected(new Set());
    api
      .identity(id)
      .then(show)
      .catch((err: unknown) => setError(describeError(err, `load identity #${id}`)));
    api
      .people()
      .then((state) => setEnrolled(state.enabled ? state.people.map((p) => p.name) : []))
      .catch(() => setEnrolled([]));
    api
      .identities()
      .then((state) => setOthers(state.identities.filter((i) => i.id !== id && i.status !== "known")))
      .catch(() => setOthers([]));
  }, [id, show]);

  const run = async (doing: string, action: () => Promise<void>) => {
    setBusy(true);
    setNote(null);
    try {
      await action();
    } catch (err) {
      setError(describeError(err, doing));
    } finally {
      setBusy(false);
    }
  };

  if (error && detail === null) {
    return (
      <main className="page identities-page">
        <a className="id-back" href="#/identities">
          ← Identities
        </a>
        <p className="page-error">{error}</p>
      </main>
    );
  }
  if (detail === null) {
    return (
      <main className="page identities-page">
        <p className="page-loading">Loading…</p>
      </main>
    );
  }

  const rename = () =>
    run("rename it", async () => {
      const trimmed = name.trim();
      show(await api.changeIdentity(id, trimmed ? { name: trimmed } : { clear_name: true }));
      setNote(trimmed ? `Named ${trimmed}.` : "Name cleared.");
    });
  const setStatus = (status: "unknown" | "suspicious" | "familiar") =>
    run("change the status", async () => {
      show(await api.changeIdentity(id, { status }));
    });
  const promote = () =>
    run("make it a known person", async () => {
      const result = await api.promoteIdentity(id, person.trim());
      show(await api.identity(id));
      setNote(
        `${detail.label} is ${result.person} now: ${plural(result.faces_added, "face")} added to their references.`,
      );
      setPerson("");
    });
  const demote = () =>
    run("take it back", async () => {
      show(await api.demoteIdentity(id));
      setNote("Not a known person any more; the faces it gave were taken back.");
    });
  const merge = (other: number) =>
    run("merge", async () => {
      show(await api.mergeIdentities([other], id));
      setNote(`#${other} merged into this one.`);
    });
  const move = (sighting: number, target: { into?: number | null; person?: string }) =>
    run("move the sighting", async () => {
      const moved = await api.moveSighting(sighting, target);
      setNote(`Moved to ${moved.label}; move it back from there if that was wrong.`);
      show(await api.identity(id));
    });
  const split = () =>
    run("split", async () => {
      const created = await api.splitIdentity(id, [...selected]);
      window.location.hash = `#/identities/${created.id}`;
    });
  const remove = () => {
    const sure = window.confirm(
      `Delete ${detail.label}? Every face crop, embedding and sighting kept for it is removed. ` +
        "The clips themselves stay.",
    );
    if (!sure) return;
    void run("delete it", async () => {
      await api.deleteIdentity(id);
      window.location.hash = "#/identities";
    });
  };

  const known = detail.status === "known";
  return (
    <main className="page identities-page">
      <a className="id-back" href="#/identities">
        ← Identities
      </a>
      <section className="card id-head">
        <Face src={detail.face} size="large" />
        <div className="id-head-info">
          <h1 className="page-title id-head-title">
            {detail.label} <StatusBadge status={detail.status} />
          </h1>
          {detail.name && <span className="muted">Stranger #{detail.id}</span>}
          <span className="muted id-item-stats">
            {plural(detail.sightings, "sighting")} on {plural(detail.days, "day")}
          </span>
          <span className="muted id-item-stats">
            first {when(detail.first_seen)} · last {when(detail.last_seen)}
          </span>
          {detail.weak && (
            <span className="muted id-item-stats">
              Small faces only: nothing joins it automatically; merge by hand.
            </span>
          )}
          {detail.near_known && !known && (
            <span className="muted id-item-stats">Close to someone enrolled: maybe one of yours.</span>
          )}
          <span className="id-cameras">
            {detail.cameras.map((camera) => (
              <span key={camera} className="chip chip-zone">
                {camera}
              </span>
            ))}
          </span>
        </div>
      </section>

      {note && <p className="person-note">{note}</p>}
      {error && <p className="page-error">{error}</p>}

      <section className="card id-actions">
        <form
          className="person-add"
          onSubmit={(event) => {
            event.preventDefault();
            void rename();
          }}
        >
          <label className="person-add-label" htmlFor="identity-name">
            Name (only here and for a local judge)
          </label>
          <input
            id="identity-name"
            value={name}
            maxLength={40}
            placeholder={`Stranger #${detail.id}`}
            autoComplete="off"
            onChange={(event) => setName(event.target.value)}
          />
          <button type="submit" className="ghost" disabled={busy || name.trim() === (detail.name ?? "")}>
            Save
          </button>
        </form>
        {!known && (
          <div className="id-status-actions" role="group" aria-label="Status">
            {detail.status !== "familiar" && (
              <button type="button" className="ghost" disabled={busy} onClick={() => void setStatus("familiar")}>
                Familiar: don't alert
              </button>
            )}
            {detail.status !== "suspicious" && (
              <button type="button" className="ghost danger" disabled={busy} onClick={() => void setStatus("suspicious")}>
                Mark suspicious
              </button>
            )}
            {detail.status !== "unknown" && (
              <button type="button" className="ghost" disabled={busy} onClick={() => void setStatus("unknown")}>
                Unmark
              </button>
            )}
          </div>
        )}
        {detail.status === "familiar" && (
          <p className="muted person-note">
            Familiar: events with only household and familiar people do not wake anyone; critical
            ones still do.
          </p>
        )}
        {!known ? (
          <form
            className="person-add"
            onSubmit={(event) => {
              event.preventDefault();
              void promote();
            }}
          >
            <label className="person-add-label" htmlFor="identity-person">
              This is someone we know: their faces join that person
            </label>
            <input
              id="identity-person"
              list="identity-people"
              value={person}
              maxLength={40}
              placeholder="Name of someone you know"
              autoComplete="off"
              onChange={(event) => setPerson(event.target.value)}
            />
            <datalist id="identity-people">
              {enrolled.map((existing) => (
                <option key={existing} value={existing} />
              ))}
            </datalist>
            <button type="submit" className="ghost" disabled={busy || !person.trim()}>
              Known
            </button>
          </form>
        ) : (
          <div className="id-status-actions">
            <p className="muted person-note">
              Known as {detail.person}: faces like this are recognized as that person now.
            </p>
            <button type="button" className="ghost" disabled={busy} onClick={() => void demote()}>
              Not {detail.person}: take back
            </button>
          </div>
        )}
        <div className="id-status-actions">
          <button type="button" className="ghost danger" disabled={busy} onClick={remove}>
            Delete identity
          </button>
        </div>
      </section>

      {detail.similar.length > 0 && (
        <section className="id-section">
          <h2 className="id-section-title">Looks like</h2>
          <p className="muted id-item-stats">
            Merge only when you are sure it is the same person: two people in one identity make
            visits that never happened.
          </p>
          <ul className="id-similar">
            {detail.similar.map((other) => (
              <li key={other.id} className="card id-similar-item">
                <a href={`#/identities/${other.id}`} className="id-similar-link">
                  <Face src={other.face} />
                  <span>
                    {other.label}
                    <span className="muted id-item-stats">
                      {" "}
                      {other.score.toFixed(2)}
                      {other.weak ? " · small faces" : ""}
                    </span>
                  </span>
                </a>
                <button type="button" className="ghost" disabled={busy} onClick={() => void merge(other.id)}>
                  Same person
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="id-section">
        <div className="id-section-head">
          <h2 className="id-section-title">Sightings</h2>
          {detail.sightings_list.length > 1 && (
            <button
              type="button"
              className="ghost"
              onClick={() => {
                setSelecting((on) => !on);
                setSelected(new Set());
              }}
            >
              {selecting ? "Cancel" : "Split…"}
            </button>
          )}
        </div>
        {selecting && (
          <div className="id-split-bar">
            <span className="muted id-item-stats">Select the sightings that are someone else.</span>
            <button
              type="button"
              className="primary"
              disabled={busy || selected.size === 0 || selected.size === detail.sightings_list.length}
              onClick={() => void split()}
            >
              Split off {selected.size}
            </button>
          </div>
        )}
        <ul className="id-sightings">
          {detail.sightings_list.map((sighting) => (
            <SightingRow
              key={sighting.id}
              sighting={sighting}
              selecting={selecting}
              selected={selected.has(sighting.id)}
              others={others}
              people={enrolled}
              busy={busy}
              onToggle={() =>
                setSelected((current) => {
                  const next = new Set(current);
                  if (next.has(sighting.id)) next.delete(sighting.id);
                  else next.add(sighting.id);
                  return next;
                })
              }
              onMove={(target) => void move(sighting.id, target)}
            />
          ))}
        </ul>
      </section>
    </main>
  );
}

export function IdentitiesPage({ id }: { id: number | null }) {
  return id === null ? <IdentityList /> : <IdentityView id={id} />;
}
