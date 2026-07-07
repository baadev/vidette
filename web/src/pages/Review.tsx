import { useEffect, useMemo, useState } from "react";
import {
  api,
  exportDownloadUrl,
  segmentFileUrl,
  type Camera,
  type ExportJob,
  type HourBucket,
  type SegmentInfo,
} from "../api";
import "./pages.css";

const HOUR_SECONDS = 3600;
const EXPORT_POLL_MS = 1000;

const DOCS_URL = "https://github.com/baadev/vidette/blob/main/docs/getting-started.md";

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** Today's date as a UTC "YYYY-MM-DD" string (the API buckets days by UTC). */
function todayUtc(): string {
  return new Date().toISOString().slice(0, 10);
}

/** The 24 UTC hour-start epoch timestamps of a "YYYY-MM-DD" day. */
function hourStartsForDay(day: string): number[] {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day);
  if (!m) return [];
  const base = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])) / 1000;
  return Array.from({ length: 24 }, (_, i) => base + i * HOUR_SECONDS);
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = "B";
  for (const u of units) {
    if (value < 1024) break;
    value /= 1024;
    unit = u;
  }
  return `${value >= 10 ? Math.round(value) : value.toFixed(1)} ${unit}`;
}

function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${pad2(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${pad2(Math.floor((s % 3600) / 60))}m`;
}

/** Local wall-clock time (HH:MM:SS) of an epoch timestamp. */
function formatLocalClock(ts: number): string {
  const d = new Date(ts * 1000);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

/** Epoch seconds → value for an `<input type="datetime-local">` (local time). */
function toLocalInputValue(ts: number): string {
  const d = new Date(ts * 1000);
  return (
    `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}` +
    `T${pad2(d.getHours())}:${pad2(d.getMinutes())}`
  );
}

/** `<input type="datetime-local">` value (local time) → epoch seconds, or null. */
function parseLocalInputValue(value: string): number | null {
  if (!value) return null;
  const ms = new Date(value).getTime();
  return Number.isNaN(ms) ? null : Math.floor(ms / 1000);
}

function describeError(err: unknown, doing: string): string {
  const detail = err instanceof Error ? err.message : String(err);
  return `Could not ${doing} (${detail}). Check that the Vidette server is running, then retry.`;
}

/**
 * Review page: pick a camera and a (UTC) day, scan the 24-hour recording
 * density strip, drill into an hour's segments, play them back, and export a
 * clip as MP4. Hour labels and all displayed times use the browser's local
 * timezone; the day/hour buckets themselves are UTC (matching the API).
 */
export function ReviewPage() {
  const [cameras, setCameras] = useState<Camera[] | null>(null);
  const [camerasError, setCamerasError] = useState<string | null>(null);
  const [camera, setCamera] = useState<string>("");
  const [day, setDay] = useState<string>(todayUtc);

  const [buckets, setBuckets] = useState<Map<number, HourBucket> | null>(null);
  const [summaryError, setSummaryError] = useState<string | null>(null);

  const [hourStart, setHourStart] = useState<number | null>(null);
  const [segments, setSegments] = useState<SegmentInfo[] | null>(null);
  const [segmentsError, setSegmentsError] = useState<string | null>(null);
  const [playingId, setPlayingId] = useState<number | null>(null);

  const [exportFrom, setExportFrom] = useState<string>("");
  const [exportTo, setExportTo] = useState<string>("");
  const [job, setJob] = useState<ExportJob | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);

  const hours = useMemo(() => hourStartsForDay(day), [day]);
  const timezone = useMemo(() => Intl.DateTimeFormat().resolvedOptions().timeZone, []);

  // Load cameras once; default the selection to the first camera.
  useEffect(() => {
    let cancelled = false;
    api
      .cameras()
      .then((cams) => {
        if (cancelled) return;
        setCameras(cams);
        setCamera((current) => current || (cams[0]?.id ?? ""));
      })
      .catch((err: unknown) => {
        if (!cancelled) setCamerasError(describeError(err, "load cameras"));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Load the per-hour summary whenever camera or day changes.
  useEffect(() => {
    if (!camera || hours.length === 0) return;
    let cancelled = false;
    setBuckets(null);
    setSummaryError(null);
    setHourStart(null);
    setSegments(null);
    setPlayingId(null);
    api
      .summary(camera, day)
      .then((rows) => {
        if (cancelled) return;
        setBuckets(new Map(rows.map((b) => [b.hour_start_ts, b])));
      })
      .catch((err: unknown) => {
        if (!cancelled) setSummaryError(describeError(err, "load the recording summary"));
      });
    return () => {
      cancelled = true;
    };
  }, [camera, day, hours]);

  // Load segments for the selected hour.
  useEffect(() => {
    if (!camera || hourStart === null) return;
    let cancelled = false;
    setSegments(null);
    setSegmentsError(null);
    setPlayingId(null);
    api
      .recordings(camera, hourStart, hourStart + HOUR_SECONDS)
      .then((segs) => {
        if (!cancelled) setSegments(segs);
      })
      .catch((err: unknown) => {
        if (!cancelled) setSegmentsError(describeError(err, "load recordings for this hour"));
      });
    return () => {
      cancelled = true;
    };
  }, [camera, hourStart]);

  // Poll a pending export job every second until it settles.
  useEffect(() => {
    if (!job || job.state === "done" || job.state === "error") return;
    const id = job.id;
    let cancelled = false;
    const timer = window.setInterval(() => {
      api
        .exportStatus(id)
        .then((j) => {
          if (!cancelled) setJob(j);
        })
        .catch((err: unknown) => {
          if (cancelled) return;
          window.clearInterval(timer);
          setExportError(describeError(err, "check the export status"));
        });
    }, EXPORT_POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [job?.id, job?.state]); // eslint-disable-line react-hooks/exhaustive-deps

  const selectHour = (hs: number): void => {
    setHourStart(hs);
    setExportFrom(toLocalInputValue(hs));
    setExportTo(toLocalInputValue(hs + HOUR_SECONDS));
    setJob(null);
    setExportError(null);
  };

  const startExport = (): void => {
    setExportError(null);
    setJob(null);
    if (!camera) {
      setExportError("Select a camera first.");
      return;
    }
    const from = parseLocalInputValue(exportFrom);
    const to = parseLocalInputValue(exportTo);
    if (from === null || to === null) {
      setExportError("Fill in both the start and end times, then try again.");
      return;
    }
    if (to <= from) {
      setExportError("The end time must be after the start time — adjust the range.");
      return;
    }
    api
      .createExport(camera, from, to)
      .then(setJob)
      .catch((err: unknown) => setExportError(describeError(err, "start the export")));
  };

  return (
    <main className="page review-page">
      <header className="page-header">
        <h1 className="page-title">Review</h1>
        <p className="kbd-hint">
          Times shown in your local timezone ({timezone}); days and hour buckets are UTC.
        </p>
      </header>

      {camerasError && <p className="page-error">{camerasError}</p>}

      {cameras !== null && cameras.length === 0 && (
        <div className="empty-state">
          <p>No cameras configured yet, so there is nothing to review.</p>
          <p>
            Add one to your <code>config.yaml</code> and restart the server — see the{" "}
            <a href={DOCS_URL} target="_blank" rel="noreferrer">
              getting started guide
            </a>
            .
          </p>
        </div>
      )}

      {cameras !== null && cameras.length > 0 && (
        <>
          <div className="review-controls">
            <label>
              Camera{" "}
              <select value={camera} onChange={(ev) => setCamera(ev.target.value)}>
                {cameras.map((cam) => (
                  <option key={cam.id} value={cam.id}>
                    {cam.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Day (UTC){" "}
              <input type="date" value={day} onChange={(ev) => setDay(ev.target.value)} />
            </label>
          </div>

          <section className="review-section">
            <h2>Recorded hours</h2>
            {summaryError && <p className="page-error">{summaryError}</p>}
            {!summaryError && buckets === null && <p className="page-loading">Loading summary…</p>}
            <div className="hour-strip" role="listbox" aria-label="Hours of the day">
              {hours.map((hs) => {
                const bucket = buckets?.get(hs);
                const secs = bucket?.recorded_seconds ?? 0;
                const pct =
                  secs <= 0 ? 0 : Math.max(6, Math.round((Math.min(secs, HOUR_SECONDS) / HOUR_SECONDS) * 100));
                const localHour = new Date(hs * 1000).getHours();
                const title =
                  secs > 0 && bucket
                    ? `${formatDuration(secs)} recorded · ${formatBytes(bucket.bytes)}`
                    : "No recordings this hour";
                return (
                  <button
                    key={hs}
                    type="button"
                    role="option"
                    aria-selected={hourStart === hs}
                    className={`hour-cell${hourStart === hs ? " hour-cell-selected" : ""}`}
                    title={title}
                    onClick={() => selectHour(hs)}
                  >
                    <span className="hour-bar">
                      <span className="hour-fill" style={{ height: `${pct}%` }} />
                    </span>
                    <span className="hour-label">{pad2(localHour)}</span>
                  </button>
                );
              })}
            </div>
          </section>

          {hourStart !== null && (
            <section className="review-section">
              <h2>
                Segments · {formatLocalClock(hourStart)}–{formatLocalClock(hourStart + HOUR_SECONDS)}
              </h2>
              {segmentsError && <p className="page-error">{segmentsError}</p>}
              {!segmentsError && segments === null && (
                <p className="page-loading">Loading segments…</p>
              )}
              {segments !== null && segments.length === 0 && (
                <p className="review-empty">No recordings in this hour.</p>
              )}
              {segments !== null && segments.length > 0 && (
                <ul className="segment-list">
                  {segments.map((seg) => (
                    <li key={seg.id}>
                      <button
                        type="button"
                        className={`segment-row${playingId === seg.id ? " segment-row-active" : ""}`}
                        onClick={() => setPlayingId(seg.id)}
                      >
                        <span className="segment-time">{formatLocalClock(seg.start_ts)}</span>
                        <span className="segment-duration">
                          {formatDuration(seg.end_ts - seg.start_ts)}
                        </span>
                        <span className="segment-size">{formatBytes(seg.size_bytes)}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              {playingId !== null && (
                <video
                  key={playingId}
                  className="review-player"
                  controls
                  autoPlay
                  src={segmentFileUrl(playingId)}
                />
              )}
            </section>
          )}

          <section className="review-section export-panel">
            <h2>Export MP4</h2>
            <div className="review-controls">
              <label>
                From{" "}
                <input
                  type="datetime-local"
                  value={exportFrom}
                  onChange={(ev) => setExportFrom(ev.target.value)}
                />
              </label>
              <label>
                To{" "}
                <input
                  type="datetime-local"
                  value={exportTo}
                  onChange={(ev) => setExportTo(ev.target.value)}
                />
              </label>
              <button
                type="button"
                className="export-button"
                onClick={startExport}
                disabled={job !== null && (job.state === "queued" || job.state === "running")}
              >
                Export MP4
              </button>
            </div>
            {exportError && <p className="page-error">{exportError}</p>}
            {job && (job.state === "queued" || job.state === "running") && (
              <p className="page-loading">Export {job.state}…</p>
            )}
            {job && job.state === "done" && (
              <p className="export-done">
                Export ready —{" "}
                <a href={exportDownloadUrl(job.id)} download>
                  download MP4
                </a>
              </p>
            )}
            {job && job.state === "error" && (
              <p className="page-error">Export failed: {job.error ?? "unknown error"}</p>
            )}
          </section>
        </>
      )}
    </main>
  );
}
