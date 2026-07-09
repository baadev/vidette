import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  api,
  snapshotUrl,
  type CameraConfigPayload,
  type CameraEntry,
  type CameraZone,
  type DiscoveredDevice,
} from "../api";
import { ZoneEditor } from "../components/ZoneEditor";
import "./pages.css";

const ID_PATTERN = /^[a-z0-9][a-z0-9-]*$/;
const RESTART_NOTE = "Applying restarts capture for a few seconds.";
const CONFIG_PATH = "/config/vidette.yaml";

type RecordMode = "continuous" | "motion" | "events" | "off";
const RECORD_MODES: RecordMode[] = ["continuous", "motion", "events", "off"];

type ProbeResult = { status: string; detail: string };

function describeError(err: unknown, doing: string): string {
  const detail = err instanceof Error ? err.message : String(err);
  return `Could not ${doing} (${detail}). Check that the Vidette server is running, then retry.`;
}

/** 409/422 responses carry an actionable problem detail from the server — show it verbatim. */
function submitErrorMessage(err: unknown, doing: string): string {
  if (err instanceof ApiError && (err.status === 409 || err.status === 422)) return err.message;
  return describeError(err, doing);
}

/** Color the inline probe result: green when healthy, red when clearly not, amber otherwise. */
function probeTone(status: string): "ok" | "warn" | "bad" {
  const value = status.toLowerCase();
  if (["ok", "online", "ready", "reachable"].includes(value)) return "ok";
  if (["error", "failed", "unreachable", "offline", "timeout"].includes(value)) return "bad";
  return "warn";
}

/** Trim the ONVIF scope URI prefix so chips stay readable. */
function scopeLabel(scope: string): string {
  return scope.replace(/^onvif:\/\/www\.onvif\.org\//, "");
}

type CameraForm = {
  id: string;
  name: string;
  adapter: "rtsp" | "onvif";
  sourceMain: string;
  sourceSub: string;
  host: string;
  port: string;
  username: string;
  password: string;
  recordMode: RecordMode;
  detectEnabled: boolean;
};

const EMPTY_FORM: CameraForm = {
  id: "",
  name: "",
  adapter: "rtsp",
  sourceMain: "",
  sourceSub: "",
  host: "",
  port: "",
  username: "",
  password: "",
  recordMode: "continuous",
  detectEnabled: false,
};

function optionString(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number") return String(value);
  return "";
}

/** Prefill the form from an existing camera's config (for edit mode). */
function configToForm(id: string, config: CameraConfigPayload): CameraForm {
  const options = config.options ?? {};
  const mode = RECORD_MODES.find((m) => m === config.record?.mode);
  return {
    id,
    name: config.name ?? "",
    adapter: config.adapter === "onvif" ? "onvif" : "rtsp",
    sourceMain: config.source?.main ?? "",
    sourceSub: config.source?.sub ?? "",
    host: optionString(options["host"]),
    port: optionString(options["port"]),
    username: optionString(options["username"]),
    password: optionString(options["password"]),
    recordMode: mode ?? "continuous",
    detectEnabled: config.detect?.enabled ?? false,
  };
}

/**
 * Turn the form back into a config payload. `base` (the config being edited)
 * is kept underneath so fields the form does not manage — zones, understand,
 * detect fps/resolution, extra adapter options — survive an edit round-trip.
 */
function buildConfig(form: CameraForm, base: CameraConfigPayload | null): CameraConfigPayload {
  const name = form.name.trim();
  const config: CameraConfigPayload = {
    ...(base ?? {}),
    adapter: form.adapter,
    name: name ? name : undefined,
    record: { mode: form.recordMode },
    detect: { ...(base?.detect ?? {}), enabled: form.detectEnabled },
  };
  if (form.adapter === "rtsp") {
    const sub = form.sourceSub.trim();
    config.source = { main: form.sourceMain.trim(), ...(sub ? { sub } : {}) };
  } else {
    const options: Record<string, unknown> = { ...(base?.options ?? {}) };
    options["host"] = form.host.trim();
    const port = form.port.trim();
    if (port) options["port"] = Number(port);
    else delete options["port"];
    if (form.username) options["username"] = form.username;
    else delete options["username"];
    if (form.password) options["password"] = form.password;
    else delete options["password"];
    config.options = options;
  }
  return config;
}

type CameraRowProps = {
  entry: CameraEntry;
  onEdit: (entry: CameraEntry) => void;
  onZones: (entry: CameraEntry) => void;
  /** Called after a successful delete so the parent re-fetches the list. */
  onDeleted: () => void;
};

/**
 * One camera row: snapshot thumbnail, identity + source badge, config chips,
 * probe result, and actions. File-defined cameras are read-only here — the row
 * says so instead of hiding the fact.
 */
function CameraRow({ entry, onEdit, onZones, onDeleted }: CameraRowProps) {
  const [snapTick, setSnapTick] = useState(() => Date.now());
  const [snapFailed, setSnapFailed] = useState(false);
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [probing, setProbing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [confirmText, setConfirmText] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [rowError, setRowError] = useState<string | null>(null);

  const name = entry.config.name?.trim() ? entry.config.name : entry.id;
  const recordMode = entry.config.record?.mode ?? "continuous";
  const detectOn = entry.config.detect?.enabled ?? false;
  const zoneCount = Object.keys(entry.config.zones ?? {}).length;

  const runProbe = () => {
    setProbing(true);
    setRowError(null);
    // A probe is also the natural moment to retry the snapshot.
    setSnapFailed(false);
    setSnapTick(Date.now());
    api
      .probeCamera(entry.id)
      .then(setProbe)
      .catch((err: unknown) => setRowError(describeError(err, `probe ${entry.id}`)))
      .finally(() => setProbing(false));
  };

  const confirmDelete = () => {
    if (confirmText !== entry.id) return;
    setDeleting(true);
    setRowError(null);
    api
      .deleteCamera(entry.id)
      .then(onDeleted)
      .catch((err: unknown) => {
        setDeleting(false);
        setRowError(submitErrorMessage(err, `delete ${entry.id}`));
      });
  };

  return (
    <li className="cam-row">
      <div className="cam-thumb">
        {snapFailed ? (
          <span className="cam-thumb-empty">no snapshot</span>
        ) : (
          <img
            src={`${snapshotUrl(entry.id)}?t=${snapTick}`}
            alt={`Latest snapshot from ${name}`}
            loading="lazy"
            onError={() => setSnapFailed(true)}
          />
        )}
      </div>

      <div className="cam-info">
        <div className="cam-title">
          <span className="cam-name">{name}</span>
          <code className="cam-id">{entry.id}</code>
          <span className={`cam-source${entry.source === "managed" ? " cam-source-managed" : ""}`}>
            {entry.source === "managed" ? "managed" : "config file"}
          </span>
        </div>
        <div className="cam-meta">
          <span className="chip">{entry.config.adapter}</span>
          <span className="chip chip-zone">record: {recordMode}</span>
          <span className="chip chip-zone">detect {detectOn ? "on" : "off"}</span>
          {zoneCount > 0 && (
            <span className="chip chip-zone">
              {zoneCount} zone{zoneCount === 1 ? "" : "s"}
            </span>
          )}
        </div>
        {probe && (
          <p className={`cam-probe cam-probe-${probeTone(probe.status)}`}>
            {probe.status}: {probe.detail}
          </p>
        )}
        {!entry.editable && (
          <p className="hint">
            defined in <code>{CONFIG_PATH}</code> — edit the file
          </p>
        )}
        {rowError && <p className="page-error cam-row-error">{rowError}</p>}
        {confirming && (
          <div className="cam-confirm">
            <label className="cam-confirm-label" htmlFor={`cam-confirm-${entry.id}`}>
              Deleting stops capture and removes this camera's config (recordings stay on disk).
              Type the camera id (<code>{entry.id}</code>) to confirm:
            </label>
            <div className="cam-confirm-row">
              <input
                id={`cam-confirm-${entry.id}`}
                value={confirmText}
                onChange={(ev) => setConfirmText(ev.target.value)}
                placeholder={entry.id}
                autoComplete="off"
              />
              <button
                type="button"
                className="cam-danger"
                onClick={confirmDelete}
                disabled={confirmText !== entry.id || deleting}
              >
                {deleting ? "Deleting…" : "Delete"}
              </button>
              <button
                type="button"
                className="ghost"
                onClick={() => {
                  setConfirming(false);
                  setConfirmText("");
                  setRowError(null);
                }}
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>

      <div className="cam-actions">
        <button type="button" className="ghost" onClick={runProbe} disabled={probing}>
          {probing ? "Probing…" : "Probe"}
        </button>
        <button type="button" className="ghost" onClick={() => onZones(entry)}>
          Zones
        </button>
        {entry.editable && (
          <>
            <button type="button" className="ghost" onClick={() => onEdit(entry)}>
              Edit
            </button>
            <button
              type="button"
              className="ghost cam-delete"
              onClick={() => {
                setConfirming(true);
                setConfirmText("");
              }}
              disabled={confirming}
            >
              Delete
            </button>
          </>
        )}
      </div>
    </li>
  );
}

/**
 * Camera management (managed cameras live in the server's own store; cameras
 * from /config/vidette.yaml are listed read-only). Add, edit, probe and delete
 * cameras, discover ONVIF devices, and draw detection zones.
 */
export function CamerasPage() {
  const [entries, setEntries] = useState<CameraEntry[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingBase, setEditingBase] = useState<CameraConfigPayload | null>(null);
  const [form, setForm] = useState<CameraForm>(EMPTY_FORM);
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [lastResult, setLastResult] = useState<{
    id: string;
    action: "added" | "updated";
    warnings: string[];
  } | null>(null);

  const [discovering, setDiscovering] = useState(false);
  const [devices, setDevices] = useState<DiscoveredDevice[] | null>(null);
  const [discoverError, setDiscoverError] = useState<string | null>(null);

  const [zoneTarget, setZoneTarget] = useState<CameraEntry | null>(null);

  const refresh = useCallback(() => {
    setRefreshing(true);
    api
      .configCameras()
      .then((list) => {
        setEntries(list);
        setListError(null);
      })
      .catch((err: unknown) => setListError(describeError(err, "load cameras")))
      .finally(() => setRefreshing(false));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const openCreate = () => {
    setForm(EMPTY_FORM);
    setEditingId(null);
    setEditingBase(null);
    setFormError(null);
    setLastResult(null);
    setFormOpen(true);
  };

  const openEdit = (entry: CameraEntry) => {
    setForm(configToForm(entry.id, entry.config));
    setEditingId(entry.id);
    setEditingBase(entry.config);
    setFormError(null);
    setLastResult(null);
    setFormOpen(true);
  };

  const closeForm = () => {
    setFormOpen(false);
    setEditingId(null);
    setEditingBase(null);
    setFormError(null);
  };

  const idValid = ID_PATTERN.test(form.id);

  const validate = (): string | null => {
    if (editingId === null && !idValid) {
      return "Camera id must be lowercase letters, digits and dashes, starting with a letter or digit (e.g. front-door).";
    }
    if (form.adapter === "rtsp" && !form.sourceMain.trim()) {
      return "The main RTSP stream URL is required.";
    }
    if (form.adapter === "onvif" && !form.host.trim()) {
      return "The ONVIF host is required.";
    }
    if (form.adapter === "onvif" && form.port.trim() && !/^\d+$/.test(form.port.trim())) {
      return "Port must be a number.";
    }
    return null;
  };

  const submit = () => {
    const problem = validate();
    if (problem) {
      setFormError(problem);
      return;
    }
    setSubmitting(true);
    setFormError(null);
    const config = buildConfig(form, editingBase);
    const request = editingId
      ? api.updateCamera(editingId, config)
      : api.createCamera(form.id, config);
    const action = editingId ? ("updated" as const) : ("added" as const);
    request
      .then((saved) => {
        setLastResult({ id: saved.id, action, warnings: saved.warnings ?? [] });
        closeForm();
        refresh();
      })
      .catch((err: unknown) =>
        setFormError(submitErrorMessage(err, editingId ? `update ${editingId}` : "add the camera")),
      )
      .finally(() => setSubmitting(false));
  };

  const discover = () => {
    setDiscovering(true);
    setDiscoverError(null);
    api
      .discoverCameras()
      .then((result) => setDevices(result.devices))
      .catch((err: unknown) => setDiscoverError(describeError(err, "discover cameras")))
      .finally(() => setDiscovering(false));
  };

  const prefillFromDevice = (device: DiscoveredDevice) => {
    // Keep whatever was already typed into a fresh add form, but never turn an
    // edit into a create by accident.
    setForm((prev) => ({
      ...(formOpen && editingId === null ? prev : EMPTY_FORM),
      adapter: "onvif",
      host: device.address,
    }));
    setEditingId(null);
    setEditingBase(null);
    setFormError(null);
    setFormOpen(true);
  };

  const saveZones = async (entry: CameraEntry, zones: Record<string, CameraZone>) => {
    await api.updateCamera(entry.id, { ...entry.config, zones });
    refresh();
  };

  return (
    <main className="page cameras-page">
      <header className="page-header">
        <h1 className="page-title">Cameras</h1>
        <p className="kbd-hint">Manage cameras and detection zones.</p>
        <button
          type="button"
          className="ghost cam-head-refresh"
          onClick={refresh}
          disabled={refreshing}
        >
          {refreshing ? "Refreshing…" : "Refresh"}
        </button>
      </header>

      {listError && <p className="page-error">{listError}</p>}
      {!listError && entries === null && <p className="page-loading">Loading cameras…</p>}

      {lastResult && (
        <div className="cam-result">
          <p className="cam-result-line">
            Camera <code>{lastResult.id}</code> {lastResult.action}.
          </p>
          {lastResult.warnings.length > 0 && (
            <ul className="warnings">
              {lastResult.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {entries !== null && entries.length === 0 && (
        <div className="empty-state">
          <p>No cameras yet.</p>
          <p>
            Add one below, or define cameras in <code>{CONFIG_PATH}</code> if you prefer the file.
          </p>
        </div>
      )}

      {entries !== null && entries.length > 0 && (
        <ul className="cam-list">
          {entries.map((entry) => (
            <CameraRow
              key={entry.id}
              entry={entry}
              onEdit={openEdit}
              onZones={setZoneTarget}
              onDeleted={refresh}
            />
          ))}
        </ul>
      )}

      <section className="card cam-add-card">
        <div className="cam-add-head">
          <h2>{editingId !== null ? `Edit camera · ${editingId}` : "Add a camera"}</h2>
          <div className="cam-add-buttons">
            <button type="button" className="ghost" onClick={discover} disabled={discovering}>
              {discovering ? "Discovering…" : "Discover (ONVIF)"}
            </button>
            {!formOpen && (
              <button type="button" className="primary" onClick={openCreate}>
                Add camera
              </button>
            )}
          </div>
        </div>

        {discoverError && <p className="page-error">{discoverError}</p>}
        {devices !== null &&
          (devices.length === 0 ? (
            <p className="muted">No ONVIF devices answered on this network.</p>
          ) : (
            <ul className="cam-devices">
              {devices.map((device) => (
                <li key={`${device.address} ${device.xaddr}`} className="cam-device">
                  <div className="cam-device-info">
                    <span className="cam-device-addr">{device.address}</span>
                    {device.xaddr && <span className="muted cam-device-xaddr">{device.xaddr}</span>}
                    {device.scopes.length > 0 && (
                      <div className="cam-device-scopes">
                        {device.scopes.slice(0, 4).map((scope) => (
                          <span key={scope} className="chip chip-zone">
                            {scopeLabel(scope)}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                  <button type="button" className="ghost" onClick={() => prefillFromDevice(device)}>
                    Use
                  </button>
                </li>
              ))}
            </ul>
          ))}

        {formOpen && (
          <form
            className="cam-form"
            onSubmit={(ev) => {
              ev.preventDefault();
              submit();
            }}
          >
            <div className="cam-form-grid">
              <div className="field">
                <label htmlFor="cam-id">Camera id</label>
                <input
                  id="cam-id"
                  value={form.id}
                  onChange={(ev) => setForm({ ...form, id: ev.target.value })}
                  placeholder="front-door"
                  disabled={editingId !== null}
                  autoComplete="off"
                />
                {editingId === null && form.id !== "" && !idValid && (
                  <p className="hint cam-id-invalid">
                    lowercase letters, digits and dashes — must start with a letter or digit
                  </p>
                )}
              </div>
              <div className="field">
                <label htmlFor="cam-name">Name (optional)</label>
                <input
                  id="cam-name"
                  value={form.name}
                  onChange={(ev) => setForm({ ...form, name: ev.target.value })}
                  placeholder="Front door"
                />
              </div>
              <div className="field">
                <label htmlFor="cam-adapter">Adapter</label>
                <select
                  id="cam-adapter"
                  value={form.adapter}
                  onChange={(ev) =>
                    setForm({ ...form, adapter: ev.target.value === "onvif" ? "onvif" : "rtsp" })
                  }
                >
                  <option value="rtsp">rtsp</option>
                  <option value="onvif">onvif</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="cam-record">Record mode</label>
                <select
                  id="cam-record"
                  value={form.recordMode}
                  onChange={(ev) => {
                    const mode = RECORD_MODES.find((m) => m === ev.target.value);
                    if (mode) setForm({ ...form, recordMode: mode });
                  }}
                >
                  {RECORD_MODES.map((mode) => (
                    <option key={mode} value={mode}>
                      {mode}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {form.adapter === "rtsp" && (
              <div className="cam-form-grid">
                <div className="field">
                  <label htmlFor="cam-src-main">Main stream URL</label>
                  <input
                    id="cam-src-main"
                    value={form.sourceMain}
                    onChange={(ev) => setForm({ ...form, sourceMain: ev.target.value })}
                    placeholder="rtsp://user:pass@192.168.1.20:554/stream1"
                    autoComplete="off"
                  />
                </div>
                <div className="field">
                  <label htmlFor="cam-src-sub">Sub stream URL (optional)</label>
                  <input
                    id="cam-src-sub"
                    value={form.sourceSub}
                    onChange={(ev) => setForm({ ...form, sourceSub: ev.target.value })}
                    placeholder="rtsp://…/stream2"
                    autoComplete="off"
                  />
                </div>
              </div>
            )}

            {form.adapter === "onvif" && (
              <>
                <div className="cam-form-grid">
                  <div className="field">
                    <label htmlFor="cam-host">Host</label>
                    <input
                      id="cam-host"
                      value={form.host}
                      onChange={(ev) => setForm({ ...form, host: ev.target.value })}
                      placeholder="192.168.1.20"
                      autoComplete="off"
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="cam-port">Port (optional)</label>
                    <input
                      id="cam-port"
                      value={form.port}
                      onChange={(ev) => setForm({ ...form, port: ev.target.value })}
                      placeholder="80"
                      inputMode="numeric"
                      autoComplete="off"
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="cam-user">Username</label>
                    <input
                      id="cam-user"
                      value={form.username}
                      onChange={(ev) => setForm({ ...form, username: ev.target.value })}
                      autoComplete="off"
                    />
                  </div>
                  <div className="field">
                    <label htmlFor="cam-pass">Password</label>
                    <input
                      id="cam-pass"
                      type="password"
                      value={form.password}
                      onChange={(ev) => setForm({ ...form, password: ev.target.value })}
                      autoComplete="new-password"
                    />
                  </div>
                </div>
                <p className="hint">
                  Password is stored as given — for env indirection (
                  <code>{"${CAM_PASSWORD}"}</code>) use the YAML file.
                </p>
              </>
            )}

            <label className="cam-check">
              <input
                type="checkbox"
                checked={form.detectEnabled}
                onChange={(ev) => setForm({ ...form, detectEnabled: ev.target.checked })}
              />
              Enable detection
            </label>

            {formError && <p className="page-error">{formError}</p>}

            <div className="cam-form-actions">
              <button type="submit" className="primary" disabled={submitting}>
                {submitting
                  ? editingId
                    ? "Saving…"
                    : "Adding…"
                  : editingId
                    ? "Save changes"
                    : "Add camera"}
              </button>
              <button type="button" className="ghost" onClick={closeForm}>
                Cancel
              </button>
              <span className="hint">{RESTART_NOTE}</span>
            </div>
          </form>
        )}
      </section>

      {zoneTarget && (
        <ZoneEditor
          cameraId={zoneTarget.id}
          zones={zoneTarget.config.zones ?? {}}
          editable={zoneTarget.editable}
          onSave={zoneTarget.editable ? (zones) => saveZones(zoneTarget, zones) : undefined}
          onClose={() => setZoneTarget(null)}
        />
      )}
    </main>
  );
}
