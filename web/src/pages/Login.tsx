import { FormEvent, useState } from "react";
import { ApiError, api, type Me } from "../api";

export type LoginMode = "wizard" | "login";

export type LoginPageProps = {
  mode: LoginMode;
  onAuthed: (me: Me) => void;
};

// Mirrors the server's bootstrap rules (vidette.auth.service) so most mistakes are
// caught before a round trip; the server remains the authority and its problem-json
// `detail` is shown verbatim when it disagrees.
const MIN_PASSWORD_LENGTH = 10;
const USERNAME_RE = /^[a-z0-9_-]{3,32}$/;

function clientValidate(username: string, password: string, confirm: string): string | null {
  if (!USERNAME_RE.test(username)) {
    return "Username must be 3–32 characters of lowercase letters, digits, '-' or '_'.";
  }
  if (password.length < MIN_PASSWORD_LENGTH) {
    return `Password must be at least ${MIN_PASSWORD_LENGTH} characters — longer is better.`;
  }
  if (password !== confirm) {
    return "Passwords do not match — re-type them.";
  }
  return null;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  return "Could not reach the API — check that the vidette server is running, then try again.";
}

export function LoginPage({ mode, onAuthed }: LoginPageProps) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const wizard = mode === "wizard";

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;
    setError(null);

    if (wizard) {
      const problem = clientValidate(username, password, confirm);
      if (problem) {
        setError(problem);
        return;
      }
    }

    setSubmitting(true);
    try {
      const me = wizard
        ? await api.bootstrap(username, password)
        : await api.login(username, password);
      onAuthed(me);
    } catch (err) {
      setError(errorText(err));
      setSubmitting(false);
    }
    // On success the component unmounts — no state updates after onAuthed.
  }

  return (
    <div className="login-wrap">
      <div className="login-brand">
        <h1 className="wordmark">
          VIDE<span className="accent">TT</span>E
        </h1>
        <p className="tagline">Self-hosted video security that understands intent.</p>
      </div>

      <form className="card login-card" onSubmit={handleSubmit} noValidate>
        <h2>{wizard ? "Create your admin account" : "Sign in"}</h2>
        {wizard && (
          <p className="muted">
            No accounts exist yet. This creates the only admin — there are no default
            credentials to remove later.
          </p>
        )}

        <div className="field">
          <label htmlFor="login-username">Username</label>
          <input
            id="login-username"
            name="username"
            type="text"
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            autoFocus
            required
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            disabled={submitting}
          />
        </div>

        <div className="field">
          <label htmlFor="login-password">Password</label>
          <input
            id="login-password"
            name="password"
            type="password"
            autoComplete={wizard ? "new-password" : "current-password"}
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={submitting}
          />
          {wizard && <p className="hint">At least {MIN_PASSWORD_LENGTH} characters.</p>}
        </div>

        {wizard && (
          <div className="field">
            <label htmlFor="login-confirm">Confirm password</label>
            <input
              id="login-confirm"
              name="confirm"
              type="password"
              autoComplete="new-password"
              required
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
              disabled={submitting}
            />
          </div>
        )}

        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}

        <button type="submit" className="primary" disabled={submitting}>
          {submitting
            ? wizard
              ? "Creating account…"
              : "Signing in…"
            : wizard
              ? "Create account"
              : "Sign in"}
        </button>
      </form>
    </div>
  );
}
