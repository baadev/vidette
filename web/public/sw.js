// Vidette service worker — web-push display and click-through only. No caching:
// the app is served by the Vidette server itself, and stale security UI helps
// no one.

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    // Not JSON — show a generic notification rather than dropping a real alert.
  }
  const title = typeof payload.title === "string" && payload.title ? payload.title : "Vidette";
  const body = typeof payload.body === "string" ? payload.body : "";
  const url = typeof payload.url === "string" && payload.url ? payload.url : "/#/events";
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: "/favicon.svg",
      badge: "/favicon.svg",
      data: { url },
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/#/events";
  event.waitUntil(
    (async () => {
      // Prefer an existing Vidette window: focus it and steer it to the feed.
      const target = new URL(url, self.location.origin).href;
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of windows) {
        try {
          const focused = await client.focus();
          if (focused.url !== target && "navigate" in focused) await focused.navigate(url);
          return;
        } catch {
          // That window refused to focus or navigate — try the next, or open fresh.
        }
      }
      await self.clients.openWindow(url);
    })(),
  );
});
