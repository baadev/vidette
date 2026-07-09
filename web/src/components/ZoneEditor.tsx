import { useEffect, useRef, useState, type MouseEvent } from "react";
import { snapshotUrl, type CameraZone } from "../api";
import "../pages/pages.css";

type ZoneKind = CameraZone["kind"];

const ZONE_KINDS: ZoneKind[] = ["entry", "object", "private", "public"];

/** Overlay colors by kind: entry amber, object blue-ish, private green-ish, public muted. */
const KIND_COLOR: Record<ZoneKind, string> = {
  entry: "var(--accent)",
  object: "#5b8dd9",
  private: "var(--ok)",
  public: "var(--muted)",
};

type Draft = { name: string; kind: ZoneKind; points: [number, number][] };

type CopyState = "idle" | "copied" | "failed";

function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value));
}

/** Rough polygon center for the floating name tag. */
function centroid(points: [number, number][]): [number, number] {
  let sx = 0;
  let sy = 0;
  for (const [x, y] of points) {
    sx += x;
    sy += y;
  }
  const n = Math.max(1, points.length);
  return [sx / n, sy / n];
}

/** Ready-to-paste YAML for a camera's `zones:` block (2-decimal coordinates). */
function zonesYaml(zones: Record<string, CameraZone>): string {
  const lines = ["zones:"];
  for (const [name, zone] of Object.entries(zones)) {
    const points = zone.points.map(([x, y]) => `[${x.toFixed(2)}, ${y.toFixed(2)}]`).join(", ");
    lines.push(`  ${name}: { kind: ${zone.kind}, points: [${points}] }`);
  }
  return lines.join("\n");
}

export type ZoneEditorProps = {
  cameraId: string;
  zones: Record<string, CameraZone>;
  editable: boolean;
  /** Persist the edited zones (the parent merges them into the camera config). */
  onSave?: (zones: Record<string, CameraZone>) => Promise<void>;
  onClose: () => void;
};

/**
 * Modal zone editor: polygons drawn over the camera's latest snapshot in
 * normalized 0–1 coordinates (SVG viewBox 0 0 1 1, stretched to the image, so
 * zones survive resolution changes). Editable cameras save through `onSave`;
 * file-defined cameras get a ready-to-paste YAML snippet instead — the config
 * file stays the source of truth.
 */
export function ZoneEditor({ cameraId, zones: initialZones, editable, onSave, onClose }: ZoneEditorProps) {
  const [zones, setZones] = useState<Record<string, CameraZone>>(() => ({ ...initialZones }));
  const [dirty, setDirty] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [draftError, setDraftError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [snapTick, setSnapTick] = useState(() => Date.now());
  const [snapFailed, setSnapFailed] = useState(false);
  const [copyState, setCopyState] = useState<CopyState>("idle");
  const dialogRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    dialogRef.current?.focus();
  }, []);

  // Escape cancels an in-progress polygon first; pressed again it closes the editor.
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key !== "Escape") return;
      ev.preventDefault();
      if (draft) {
        setDraft(null);
        setDraftError(null);
      } else {
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [draft, onClose]);

  // Let the copy-button label fall back after a moment.
  useEffect(() => {
    if (copyState === "idle") return;
    const timer = window.setTimeout(() => setCopyState("idle"), 2000);
    return () => window.clearTimeout(timer);
  }, [copyState]);

  /** While drawing, clicks append a normalized point; otherwise they clear the selection. */
  const stageClick = (ev: MouseEvent<SVGSVGElement>) => {
    if (!draft) {
      setSelected(null);
      return;
    }
    const rect = ev.currentTarget.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    const x = clamp01((ev.clientX - rect.left) / rect.width);
    const y = clamp01((ev.clientY - rect.top) / rect.height);
    const point: [number, number] = [Number(x.toFixed(4)), Number(y.toFixed(4))];
    setDraft({ ...draft, points: [...draft.points, point] });
    setDraftError(null);
  };

  const startDraft = () => {
    setDraft({ name: "", kind: "entry", points: [] });
    setSelected(null);
    setDraftError(null);
  };

  const commitDraft = () => {
    if (!draft) return;
    const name = draft.name.trim();
    if (!name) {
      setDraftError("Give the zone a name first.");
      return;
    }
    if (Object.prototype.hasOwnProperty.call(zones, name)) {
      setDraftError(`A zone named "${name}" already exists — pick another name.`);
      return;
    }
    if (draft.points.length < 3) {
      setDraftError("A zone needs at least 3 points — click the image to add more.");
      return;
    }
    setZones({ ...zones, [name]: { kind: draft.kind, points: draft.points } });
    setDirty(true);
    setSelected(name);
    setDraft(null);
    setDraftError(null);
  };

  const deleteSelected = () => {
    if (selected === null) return;
    const next = { ...zones };
    delete next[selected];
    setZones(next);
    setSelected(null);
    setDirty(true);
  };

  const save = () => {
    if (!onSave) return;
    setSaving(true);
    setSaveError(null);
    onSave(zones)
      .then(() => onClose())
      .catch((err: unknown) => {
        setSaving(false);
        setSaveError(err instanceof Error ? err.message : String(err));
      });
  };

  const copyYaml = () => {
    navigator.clipboard
      .writeText(zonesYaml(zones))
      .then(() => setCopyState("copied"))
      .catch(() => setCopyState("failed"));
  };

  const zoneEntries = Object.entries(zones);
  const copyLabel =
    copyState === "copied"
      ? "Copied"
      : copyState === "failed"
        ? "Copy failed — select it"
        : "Copy YAML";

  return (
    <div className="zone-backdrop" role="presentation">
      <div
        ref={dialogRef}
        className="zone-editor"
        role="dialog"
        aria-modal="true"
        aria-label={`Zones for ${cameraId}`}
        tabIndex={-1}
      >
        <header className="zone-head">
          <h2>
            Zones · <code>{cameraId}</code>
          </h2>
          <div className="zone-head-actions">
            <button
              type="button"
              className="ghost"
              onClick={() => {
                setSnapFailed(false);
                setSnapTick(Date.now());
              }}
            >
              Refresh snapshot
            </button>
            <button type="button" className="ghost" onClick={onClose}>
              Close
            </button>
          </div>
        </header>

        <div className="zone-body">
          <div className="zone-stage-wrap">
            <div className={`zone-stage${draft ? " zone-stage-drawing" : ""}`}>
              {snapFailed ? (
                <div className="zone-stage-empty">
                  no snapshot yet — zones are drawn on a blank 16:9 frame
                </div>
              ) : (
                <img
                  src={`${snapshotUrl(cameraId)}?t=${snapTick}`}
                  alt={`Latest snapshot from ${cameraId}`}
                  onError={() => setSnapFailed(true)}
                />
              )}
              <svg
                className="zone-svg"
                viewBox="0 0 1 1"
                preserveAspectRatio="none"
                onClick={stageClick}
                role="presentation"
              >
                {zoneEntries.map(([name, zone]) => (
                  <polygon
                    key={name}
                    points={zone.points.map((p) => p.join(",")).join(" ")}
                    style={{ fill: KIND_COLOR[zone.kind], stroke: KIND_COLOR[zone.kind] }}
                    fillOpacity={selected === name ? 0.35 : 0.16}
                    strokeWidth={selected === name ? 2.5 : 1.5}
                    vectorEffect="non-scaling-stroke"
                    onClick={(ev) => {
                      if (draft) return; // drawing: let the click fall through and add a point
                      ev.stopPropagation();
                      setSelected((current) => (current === name ? null : name));
                    }}
                  />
                ))}
                {draft && draft.points.length >= 2 && (
                  <polyline
                    points={draft.points.map((p) => p.join(",")).join(" ")}
                    fill="none"
                    style={{ stroke: KIND_COLOR[draft.kind] }}
                    strokeWidth={2}
                    strokeDasharray="6 4"
                    vectorEffect="non-scaling-stroke"
                  />
                )}
              </svg>
              {zoneEntries.map(([name, zone]) => {
                const [cx, cy] = centroid(zone.points);
                return (
                  <span
                    key={name}
                    className="zone-tag"
                    style={{ left: `${cx * 100}%`, top: `${cy * 100}%` }}
                  >
                    {name}
                  </span>
                );
              })}
              {draft?.points.map((point, index) => (
                <span
                  key={index} // points are append/pop only, so the index is stable
                  className="zone-dot"
                  style={{
                    left: `${point[0] * 100}%`,
                    top: `${point[1] * 100}%`,
                    background: KIND_COLOR[draft.kind],
                  }}
                />
              ))}
            </div>
            <p className="hint">
              {draft
                ? `Click the image to outline the zone — ${draft.points.length} point${
                    draft.points.length === 1 ? "" : "s"
                  } so far, 3 needed to close.`
                : "Coordinates are stored normalized (0–1), so zones survive resolution changes."}
            </p>
          </div>

          <div className="zone-side">
            {draft ? (
              <div className="zone-draft">
                <h3>New zone</h3>
                <div className="field">
                  <label htmlFor="zone-name">Name</label>
                  <input
                    id="zone-name"
                    value={draft.name}
                    onChange={(ev) => setDraft({ ...draft, name: ev.target.value })}
                    placeholder="driveway"
                    autoComplete="off"
                  />
                </div>
                <div className="field">
                  <label htmlFor="zone-kind">Kind</label>
                  <select
                    id="zone-kind"
                    value={draft.kind}
                    onChange={(ev) => {
                      const kind = ZONE_KINDS.find((k) => k === ev.target.value);
                      if (kind) setDraft({ ...draft, kind });
                    }}
                  >
                    {ZONE_KINDS.map((kind) => (
                      <option key={kind} value={kind}>
                        {kind}
                      </option>
                    ))}
                  </select>
                </div>
                {draftError && <p className="page-error zone-error">{draftError}</p>}
                <div className="zone-draft-actions">
                  <button
                    type="button"
                    className="ghost"
                    onClick={() => setDraft({ ...draft, points: draft.points.slice(0, -1) })}
                    disabled={draft.points.length === 0}
                  >
                    Undo point
                  </button>
                  <button
                    type="button"
                    className="primary"
                    onClick={commitDraft}
                    disabled={draft.points.length < 3}
                  >
                    Close polygon
                  </button>
                  <button
                    type="button"
                    className="ghost"
                    onClick={() => {
                      setDraft(null);
                      setDraftError(null);
                    }}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            ) : (
              <>
                <div className="zone-list-head">
                  <h3>Zones</h3>
                  <button type="button" className="ghost" onClick={startDraft}>
                    Add zone
                  </button>
                </div>
                {zoneEntries.length === 0 ? (
                  <p className="muted">No zones yet — add one and click the image to outline it.</p>
                ) : (
                  <ul className="zone-list">
                    {zoneEntries.map(([name, zone]) => (
                      <li key={name}>
                        <button
                          type="button"
                          className={`zone-item${selected === name ? " zone-item-selected" : ""}`}
                          onClick={() =>
                            setSelected((current) => (current === name ? null : name))
                          }
                        >
                          <span
                            className="zone-swatch"
                            style={{ background: KIND_COLOR[zone.kind] }}
                          />
                          <span className="zone-item-name">{name}</span>
                          <span className="zone-item-kind">
                            {zone.kind} · {zone.points.length} pts
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
                {selected !== null && (
                  <button type="button" className="ghost zone-delete" onClick={deleteSelected}>
                    Delete “{selected}”
                  </button>
                )}
              </>
            )}
          </div>
        </div>

        <footer className="zone-foot">
          {editable ? (
            <>
              {saveError && <p className="page-error zone-error">{saveError}</p>}
              <div className="zone-foot-row">
                <button
                  type="button"
                  className="primary"
                  onClick={save}
                  disabled={saving || !dirty || draft !== null}
                  title={dirty ? undefined : "No changes yet"}
                >
                  {saving ? "Saving…" : "Save zones"}
                </button>
                <span className="hint">Applying restarts capture for a few seconds.</span>
              </div>
            </>
          ) : (
            <>
              <p className="hint">
                this camera is defined in the config file — paste this under{" "}
                <code>cameras.{cameraId}:</code>
              </p>
              {zoneEntries.length > 0 ? (
                <pre className="zone-yaml">
                  <code>{zonesYaml(zones)}</code>
                </pre>
              ) : (
                <p className="muted">Draw zones above to generate the YAML snippet.</p>
              )}
              <div className="zone-foot-row">
                <button
                  type="button"
                  className="ghost"
                  onClick={copyYaml}
                  disabled={zoneEntries.length === 0}
                >
                  {copyLabel}
                </button>
              </div>
            </>
          )}
        </footer>
      </div>
    </div>
  );
}
