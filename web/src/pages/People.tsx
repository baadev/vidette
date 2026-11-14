import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, type PeopleState, type Person, type UploadResult } from "../api";
import "./pages.css";

// Known people (docs/people.md, ADR-0012): who the household enrolled, their reference
// faces, and the upload that adds more. Faces are found and compared on the server's own
// machine; nothing on this page talks to anything else.

function describeError(err: unknown, doing: string): string {
  const detail = err instanceof Error ? err.message : String(err);
  return `Could not ${doing}: ${detail}`;
}

function uploadSummary(result: UploadResult): string {
  const parts = [`${result.added} face${result.added === 1 ? "" : "s"} added`];
  if (result.duplicates) parts.push(`${result.duplicates} already known`);
  if (result.unclear) parts.push(`${result.unclear} too small or unclear`);
  if (result.faces_found === 0) return "No face found in that photo.";
  return `${parts.join(", ")}.`;
}

type PersonCardProps = {
  person: Person;
  canUpload: boolean;
  onChanged: (person: Person | null) => void;
};

function PersonCard({ person, canUpload, onChanged }: PersonCardProps) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const upload = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setBusy(true);
    setError(null);
    setNote(null);
    const notes: string[] = [];
    let latest: Person = person;
    try {
      for (const file of Array.from(files)) {
        const result = await api.uploadPhoto(person.id, file);
        latest = result.person;
        notes.push(`${file.name}: ${uploadSummary(result)}`);
        notes.push(...result.skipped.map((line) => `skipped ${line}`));
      }
      setNote(notes.join(" "));
      onChanged(latest);
    } catch (err) {
      setError(describeError(err, "add the photo"));
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const removeFace = async (faceId: string) => {
    setError(null);
    try {
      await api.deleteFace(person.id, faceId);
      onChanged({ ...person, faces: person.faces.filter((face) => face.id !== faceId) });
    } catch (err) {
      setError(describeError(err, "remove the face"));
    }
  };

  const removePerson = async () => {
    const sure = window.confirm(
      `Delete ${person.name}? Every photo, face and embedding stored for them is removed. ` +
        "This cannot be undone.",
    );
    if (!sure) return;
    try {
      await api.deletePerson(person.id);
      onChanged(null);
    } catch (err) {
      setError(describeError(err, `delete ${person.name}`));
    }
  };

  return (
    <li className="card person-card">
      <div className="person-head">
        <h2 className="person-name">{person.name}</h2>
        <span className="muted person-stats">
          {person.enrolled} from photos · {person.confirmed} confirmed · threshold{" "}
          {person.threshold.toFixed(2)}
          {person.rejected > 0 ? ` · ${person.rejected} wrong match${person.rejected === 1 ? "" : "es"} noted` : ""}
        </span>
      </div>
      {person.faces.length === 0 ? (
        <p className="muted person-empty">No faces yet: add a photo where the face is clear.</p>
      ) : (
        <ul className="face-grid" aria-label={`${person.name}'s reference faces`}>
          {person.faces.map((face) => (
            <li key={face.id} className={`face-tile face-kind-${face.kind}`}>
              <img src={face.url} alt="" loading="lazy" />
              <button
                type="button"
                className="face-remove"
                aria-label="Remove this face"
                title="Remove this face"
                onClick={() => void removeFace(face.id)}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="person-actions">
        <label className={`ghost-label${!canUpload || busy ? " disabled" : ""}`}>
          {busy ? "Looking for faces…" : "Add photos"}
          <input
            ref={fileInput}
            type="file"
            accept="image/jpeg,image/png,image/webp"
            multiple
            disabled={!canUpload || busy}
            onChange={(event) => void upload(event.target.files)}
          />
        </label>
        <button type="button" className="ghost danger" onClick={() => void removePerson()}>
          Delete
        </button>
      </div>
      {note && <p className="person-note">{note}</p>}
      {error && <p className="page-error">{error}</p>}
    </li>
  );
}

export function PeoplePage() {
  const [state, setState] = useState<PeopleState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [adding, setAdding] = useState(false);

  const load = useCallback(() => {
    api
      .people()
      .then((next) => {
        setState(next);
        setError(null);
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.status === 403) {
          setError("Only an admin can see known people.");
        } else {
          setError(describeError(err, "load known people"));
        }
      });
  }, []);

  useEffect(load, [load]);

  const add = async () => {
    const trimmed = name.trim();
    if (!trimmed) return;
    setAdding(true);
    try {
      const person = await api.createPerson(trimmed);
      setState((current) =>
        current === null
          ? current
          : {
              ...current,
              people: [...current.people.filter((p) => p.id !== person.id), person].sort((a, b) =>
                a.id.localeCompare(b.id),
              ),
            },
      );
      setName("");
    } catch (err) {
      setError(describeError(err, "add the person"));
    } finally {
      setAdding(false);
    }
  };

  const changed = (id: string) => (person: Person | null) =>
    setState((current) =>
      current === null
        ? current
        : {
            ...current,
            people:
              person === null
                ? current.people.filter((p) => p.id !== id)
                : current.people.map((p) => (p.id === id ? person : p)),
          },
    );

  return (
    <main className="page people-page">
      <header className="page-header">
        <h1 className="page-title">People</h1>
        <p className="kbd-hint">
          Faces of people you know, recognized on this machine. The judge is told who it saw.
        </p>
      </header>

      {error && <p className="page-error">{error}</p>}
      {!error && state === null && <p className="page-loading">Loading…</p>}

      {state !== null && !state.enabled && (
        <div className="empty-state">
          <p>Known people are off.</p>
          <p>
            Add <code>people: {"{"} enabled: true {"}"}</code> to the config, run{" "}
            <code>vidette people models</code> once, and restart.
          </p>
        </div>
      )}

      {state !== null && state.enabled && (
        <>
          {!state.available && (
            <p className="page-error">
              Face recognition is not running: {state.reason ?? "see the server log"}
            </p>
          )}
          {state.warnings.map((warning) => (
            <p key={warning} className="page-error">
              {warning}
            </p>
          ))}
          <form
            className="person-add"
            onSubmit={(event) => {
              event.preventDefault();
              void add();
            }}
          >
            <label className="person-add-label" htmlFor="person-name">
              Add a person
            </label>
            <input
              id="person-name"
              value={name}
              maxLength={40}
              placeholder="Name"
              autoComplete="off"
              onChange={(event) => setName(event.target.value)}
            />
            <button type="submit" className="primary" disabled={adding || !name.trim()}>
              Add
            </button>
          </form>

          {state.people.length === 0 ? (
            <div className="empty-state">
              <p>Nobody yet. Add a person, then a few photos where their face is clear.</p>
            </div>
          ) : (
            <ul className="person-list">
              {state.people.map((person) => (
                <PersonCard
                  key={person.id}
                  person={person}
                  canUpload={state.available}
                  onChanged={changed(person.id)}
                />
              ))}
            </ul>
          )}

          {state.not_enrolled.length > 0 && (
            <p className="muted people-footnote">
              Folders with photos but nobody enrolled: {state.not_enrolled.join(", ")}.
            </p>
          )}
          <p className="muted people-footnote">
            Photos, faces and their embeddings stay in {state.dir ?? "the people folder"} on
            this machine, readable by its owner only. Enroll only people who agreed. On events,
            “This is …” adds the face to that person; “Stranger” after a wrong match makes the
            match stricter and keeps nothing about the stranger.
          </p>
        </>
      )}
    </main>
  );
}
