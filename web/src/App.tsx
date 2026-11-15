import { useCallback, useEffect, useState } from "react";
import { UNAUTHORIZED_EVENT, api, type Me } from "./api";
import { EventsPage } from "./pages/Events";
import { IdentitiesPage } from "./pages/Identities";
import { LoginPage } from "./pages/Login";
import { PeoplePage } from "./pages/People";
import { SystemPage } from "./pages/System";

// v0.2 (ADR-0009): live view, review timeline, camera management and the setup
// wizard belonged to the NVR core, frozen at tag `nvr-core`. Frigate does those.
type Route = "events" | "people" | "identities" | "system";

const ROUTES: { route: Route; hash: string; label: string }[] = [
  { route: "events", hash: "#/events", label: "Events" },
  { route: "people", hash: "#/people", label: "People" },
  { route: "system", hash: "#/system", label: "System" },
];

function parseRoute(hash: string): Route {
  if (hash.startsWith("#/system")) return "system";
  if (hash.startsWith("#/people")) return "people";
  if (hash.startsWith("#/identities")) return "identities";
  return "events"; // default, including "" and unknown or retired hashes
}

/** `#/identities/12` → 12; anything else → null (the list). */
function parseIdentity(hash: string): number | null {
  const match = /^#\/identities\/(\d+)/.exec(hash);
  return match ? Number(match[1]) : null;
}

function useRoute(): [Route, number | null] {
  const [hash, setHash] = useState<string>(() => window.location.hash);
  useEffect(() => {
    const onHashChange = () => {
      setHash(window.location.hash);
      window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);
  return [parseRoute(hash), parseIdentity(hash)];
}

type Boot =
  | { phase: "loading" }
  | { phase: "unreachable" }
  | { phase: "wizard" }
  | { phase: "login" }
  | { phase: "authed"; user: Me };

export function App() {
  const [boot, setBoot] = useState<Boot>({ phase: "loading" });
  const [route, identity] = useRoute();
  // Identities live under the People tab: both are "who the camera saw".
  const tabRoute: Route = route === "identities" ? "people" : route;

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

  const handleAuthed = useCallback((user: Me) => {
    setBoot({ phase: "authed", user });
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
        <a className="wordmark wordmark-small" href="#/events">
          VIDE<span className="accent">TT</span>E
        </a>
        <nav className="tabs" aria-label="Primary">
          {ROUTES.map(({ route: tab, hash, label }) => (
            <a
              key={tab}
              href={hash}
              className={tabRoute === tab ? "tab active" : "tab"}
              aria-current={tabRoute === tab ? "page" : undefined}
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
        {route === "events" && <EventsPage />}
        {route === "people" && <PeoplePage />}
        {route === "identities" && <IdentitiesPage id={identity} />}
        {route === "system" && <SystemPage />}
      </main>
    </div>
  );
}
