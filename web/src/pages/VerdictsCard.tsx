import { useEffect, useState } from "react";
import { ApiError, api, type VlmBackend, type VlmOption, type VlmSettings } from "../api";

// The backend switch (docs/vlm.md): Off / Local / Cloud. Each choice says where the
// frames go; Cloud asks once more before anything is stored, because from then on
// frames from the cameras leave the house.

function usageLine(option: VlmOption): string | null {
  const usage = option.usage_today;
  if (!usage) return null;
  const money = usage.usd > 0 ? ` · $${usage.usd.toFixed(usage.usd < 0.01 ? 4 : 2)}` : "";
  return `${usage.calls} of ${usage.limit} calls today${money}`;
}

export function VerdictsCard() {
  const [settings, setSettings] = useState<VlmSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState<VlmOption | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .vlmSettings()
      .then((data) => {
        if (!cancelled) setSettings(data);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const choose = async (backend: VlmBackend) => {
    setBusy(true);
    setError(null);
    try {
      setSettings(await api.setVlmBackend(backend));
      setConfirming(null);
    } catch (err) {
      setError(err instanceof ApiError || err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const onPick = (option: VlmOption) => {
    if (!settings || !option.available || option.id === settings.backend) return;
    if (option.cloud) {
      setConfirming(option); // nothing is stored until the person says yes
      return;
    }
    void choose(option.id);
  };

  if (!settings) {
    return (
      <section className="card">
        <h2>Verdicts</h2>
        {error ? <p className="error">{error}</p> : <p className="muted">Loading…</p>}
      </section>
    );
  }

  const worker = settings.worker;
  return (
    <section className="card">
      <h2>Verdicts</h2>
      <p className="verdicts-lead">Which model reads your policies and judges incidents.</p>
      <div className="vlm-choices" role="radiogroup" aria-label="VLM backend">
        {settings.options.map((option) => {
          const selected = option.id === settings.backend;
          const pending = confirming?.id === option.id;
          const usage = usageLine(option);
          return (
            <label
              key={option.id}
              className={[
                "vlm-choice",
                selected ? "selected" : "",
                pending ? "pending" : "",
                option.available ? "" : "unavailable",
              ].join(" ")}
            >
              <input
                type="radio"
                name="vlm-backend"
                value={option.id}
                aria-label={option.label}
                checked={selected}
                disabled={busy || !option.available}
                onChange={() => onPick(option)}
              />
              <span className="vlm-choice-head">
                <span className="vlm-choice-label">{option.label}</span>
                {option.model && <span className="vlm-choice-model">{option.model}</span>}
                {selected && <span className="badge">in use</span>}
              </span>
              {option.endpoint && <span className="vlm-choice-endpoint">{option.endpoint}</span>}
              <span className={option.cloud && option.available ? "vlm-privacy cloud" : "vlm-privacy"}>
                {option.privacy}
              </span>
              {usage && <span className="vlm-choice-usage">{usage}</span>}
              {!option.available && option.reason && (
                <span className="vlm-choice-reason">Not configured: {option.reason}.</span>
              )}
            </label>
          );
        })}
      </div>

      {confirming && (
        <div className="vlm-confirm" role="alertdialog" aria-label="Confirm cloud backend">
          <p>
            <strong>Frames from your cameras will be sent to {confirming.endpoint ?? "the cloud"}.</strong>{" "}
            Every judged incident uploads {settings.frames_per_call} still frames to that
            provider, under its terms. Local models keep them at home.
          </p>
          <div className="vlm-confirm-actions">
            <button
              type="button"
              className="primary"
              disabled={busy}
              onClick={() => void choose(confirming.id)}
            >
              Send frames to the cloud
            </button>
            <button type="button" className="ghost" disabled={busy} onClick={() => setConfirming(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {error && <p className="error">{error}</p>}

      <p className="muted vlm-status">
        <span className={worker.running && settings.backend !== "none" ? "dot ok" : "dot idle"} />
        {worker.note}
        {worker.judged > 0 && (
          <>
            {" "}
            Since start: {worker.judged} judged, {worker.confirmed} alerted, {worker.dismissed}{" "}
            dismissed, {worker.undecided} without a verdict.
          </>
        )}
      </p>
      <p className="muted vlm-source">
        {settings.source === "ui" ? (
          <>
            Chosen here and stored; the config says <code>backend: {settings.config_backend}</code>.
          </>
        ) : (
          <>
            From the config (<code>understanding.vlm.backend</code>); a choice made here is stored
            and wins.
          </>
        )}
      </p>
    </section>
  );
}
