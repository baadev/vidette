import { useEffect, useState } from "react";
import { ApiError, api, type CalibrationState, type CalibrationStep } from "../api";

// Calibration from 👍/👎 (docs/calibration.md, ADR-0020): what the marks changed, why,
// and a button that takes a change back. The policy file is never touched; this list is
// the whole truth about what differs from it.

const REASONS: Record<string, string> = {
  passing: "walked past",
  car_use: "their own car",
  animal: "animal",
  vehicle: "a car",
  light: "light, shadow",
  unimportant: "not important",
  other: "other",
  none: "no reason",
};

function how(step: CalibrationStep): string {
  if (step.step_to >= 2) return "only in the digest";
  if (step.step_to === 1) return `wakes only from ${Math.round((step.bar ?? 0) * 100)} % sure`;
  return "as the policy says";
}

function why(step: CalibrationStep): string {
  const evidence = step.evidence;
  if (evidence.down === undefined) return "a 👍 on one it had kept in the digest";
  const reasons = Object.entries(evidence.reasons ?? {})
    .map(([code, count]) => `${REASONS[code] ?? code} ${count}`)
    .join(", ");
  return `${evidence.down} × 👎${reasons ? ` (${reasons})` : ""}`;
}

function when(ts: number): string {
  return new Date(ts * 1000).toLocaleString(undefined, {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  });
}

export function CalibrationCard() {
  const [state, setState] = useState<CalibrationState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);

  const load = () =>
    api
      .calibration()
      .then(setState)
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));

  useEffect(() => {
    void load();
  }, []);

  const revert = async (step: CalibrationStep) => {
    setBusy(step.id);
    setError(null);
    try {
      await api.revertCalibration(step.id);
      await load();
    } catch (err) {
      setError(err instanceof ApiError || err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  if (!state) {
    return (
      <section className="card">
        <h2>Calibration</h2>
        {error ? <p className="error">{error}</p> : <p className="muted">Loading…</p>}
      </section>
    );
  }

  const rule = state.rule;
  const active = state.steps.filter((step) => step.in_force);
  const history = state.steps.filter((step) => !step.in_force);
  return (
    <section className="card">
      <h2>Calibration</h2>
      <p className="muted">
        {rule.enabled ? (
          <>
            After {rule.marks} 👎 and no 👍 on one scenario of one camera (within {rule.window}),
            it wakes you less: first only from {Math.round(rule.bar * 100)} % sure, then only in
            the digest. A 👍 on one it kept in the digest takes that back. Critical scenarios
            ({rule.protected.length}) never leave the alerts; their bar stops at{" "}
            {Math.round(rule.protected_bar * 100)} %. Your policy file is never changed.
          </>
        ) : (
          <>
            Off (<code>calibration.enabled: false</code>): 👍/👎 only become labels.
          </>
        )}
      </p>
      {active.length === 0 ? (
        <p className="muted">Your marks have not changed anything yet.</p>
      ) : (
        <ul className="calibration-list">
          {active.map((step) => (
            <li key={step.id}>
              <span>
                <strong>{step.scenario_title}</strong> on {step.camera_name}: {how(step)}
                <span className="muted"> since {when(step.at)}, {why(step)}</span>
              </span>
              <button
                type="button"
                className="ghost"
                disabled={busy !== null}
                onClick={() => void revert(step)}
              >
                Undo
              </button>
            </li>
          ))}
        </ul>
      )}
      {history.length > 0 && (
        <details className="calibration-history">
          <summary className="muted">Earlier changes ({history.length})</summary>
          <ul>
            {history.map((step) => (
              <li key={step.id} className="muted">
                {when(step.at)} {step.scenario_title} on {step.camera_name}: {how(step)} ({why(step)})
                {step.reverted_at !== null &&
                  `, taken back ${when(step.reverted_at)} (${step.reverted_by ?? "?"})`}
              </li>
            ))}
          </ul>
        </details>
      )}
      {error && <p className="error">{error}</p>}
    </section>
  );
}
