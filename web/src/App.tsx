import { useCallback, useEffect, useState } from "react";
import { UNAUTHORIZED_EVENT, api, type Me } from "./api";
import { EventsPage } from "./pages/Events";
import { LivePage } from "./pages/Live";
import { LoginPage } from "./pages/Login";
import { ReviewPage } from "./pages/Review";
import { SetupPage } from "./pages/Setup";
import { SystemPage } from "./pages/System";

type Route = "live" | "events" | "review" | "system" | "setup";

// "setup" is deliberately absent: it is a one-time first-run flow, reachable
// right after bootstrap (and via #/setup), not a permanent nav destination.
const ROUTES: { route: Route; hash: string; label: string }[] = [
  { route: "live", hash: "#/live", label: "Live" },
  { route: "events", hash: "#/events", label: "Events" },
  { route: "review", hash: "#/review", label: "Review" },
  { route: "system", hash: "#/system", label: "System" },
];

function parseRoute(hash: string): Route {
  if (hash.startsWith("#/events")) return "events";
  if (hash.startsWith("#/review")) return "review";
  if (hash.startsWith("#/system")) return "system";
  if (hash.startsWith("#/setup")) return "setup";
  return "live"; // default, including "" and unknown hashes
}

function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  useEffect(() => {
    const onHashChange = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);
  return route;
}

type Boot =
  | { phase: "loading" }
  | { phase: "unreachable" }
  | { phase: "wizard" }
  | { phase: "login" }
  | { phase: "authed"; user: Me };

export function App() {
  const [boot, setBoot] = useState<Boot>({ phase: "loading" });
  const route = useRoute();

  const bootstrapFlow = useCallback(async () => {
    setBoot({ phase: "loading" });
    try {
      const status = await api.authStatus();
      if (!status.bootstrapped) {
        setBoot({ phase: "wizard" });
        return;
      }
      const user = await api.me();
      setBoot(user ? { phase: "authed", user } : { phase: "login" });
    } catch {
      setBoot({ phase: "unreachable" });
    }
  }, []);

  useEffect(() => {
    void bootstrapFlow();
  }, [bootstrapFlow]);

  // Any authenticated request answering 401 (session expired, revoked, server restarted)
  // drops the shell back to the login screen.
  useEffect(() => {
    const onUnauthorized = () => {
      setBoot((previous) => (previous.phase === "authed" ? { phase: "login" } : previous));
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  const handleAuthed = useCallback(
    (user: Me) => {
      // After the first-run bootstrap, walk through camera setup once.
      if (boot.phase === "wizard") window.location.hash = "#/setup";
      setBoot({ phase: "authed", user });
    },
    [boot.phase],
  );

  const handleSetupDone = useCallback(() => {
    window.location.hash = "#/live";
  }, []);

  const handleLogout = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      // The session cookie may already be gone; either way the shell locks.
    }
    setBoot({ phase: "login" });
  }, []);

  if (boot.phase === "loading") {
    return (
      <div className="boot-screen">
        <p className="muted">Contacting API…</p>
      </div>
    );
  }

  if (boot.phase === "unreachable") {
    return (
      <div className="boot-screen">
        <div className="card">
          <h2>API unreachable</h2>
          <p className="error">
            Could not reach the Vidette API — check that the server (or container) is running,
            then retry.
          </p>
          <button type="button" className="primary" onClick={() => void bootstrapFlow()}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (boot.phase === "wizard" || boot.phase === "login") {
    return <LoginPage mode={boot.phase} onAuthed={handleAuthed} />;
  }

  return (
    <div className="app">
      <header className="app-header">
        <a className="wordmark wordmark-small" href="#/live">
          VIDE<span className="accent">TT</span>E
        </a>
        <nav className="tabs" aria-label="Primary">
          {ROUTES.map(({ route: tab, hash, label }) => (
            <a
              key={tab}
              href={hash}
              className={route === tab ? "tab active" : "tab"}
              aria-current={route === tab ? "page" : undefined}
            >
              {label}
            </a>
          ))}
        </nav>
        <div className="session">
          <span className="session-user" title={`role: ${boot.user.role}`}>
            {boot.user.username}
          </span>
          <button type="button" className="ghost" onClick={() => void handleLogout()}>
            Log out
          </button>
        </div>
      </header>
      <main className="app-main">
        {route === "live" && <LivePage />}
        {route === "events" && <EventsPage />}
        {route === "review" && <ReviewPage />}
        {route === "system" && <SystemPage />}
        {route === "setup" && <SetupPage onDone={handleSetupDone} />}
      </main>
    </div>
  );
}
