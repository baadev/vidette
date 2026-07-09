// Web-push helpers: service-worker registration, the permission flow, and the
// subscribe/unsubscribe round-trips with the server. UI-free — pages own the
// presentation. Every failure surfaces as a thrown Error whose message says
// what happened and what to do next.

import { api } from "./api";

/** Where the notification service worker lives (served from web/public/). */
const SW_PATH = "/sw.js";

export type PushState = "unsupported" | "denied" | "subscribed" | "unsubscribed";

function detail(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/**
 * Decode a base64url-encoded VAPID key into the raw bytes
 * `pushManager.subscribe` expects as `applicationServerKey`.
 * (Typed `Uint8Array<ArrayBuffer>` because DOM `BufferSource` requires an
 * ArrayBuffer-backed view since TS 5.7.)
 */
export function urlBase64ToUint8Array(key: string): Uint8Array<ArrayBuffer> {
  const padded = key + "=".repeat((4 - (key.length % 4)) % 4);
  const base64 = padded.replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

/** What push looks like in this browser right now — never throws for support gaps. */
export async function getPushState(): Promise<PushState> {
  if (
    !("serviceWorker" in navigator) ||
    !("PushManager" in window) ||
    !("Notification" in window)
  ) {
    return "unsupported";
  }
  if (Notification.permission === "denied") return "denied";
  const registration = await navigator.serviceWorker.getRegistration();
  const subscription = (await registration?.pushManager.getSubscription()) ?? null;
  return subscription === null ? "unsubscribed" : "subscribed";
}

/**
 * Register the service worker (idempotent), ask for permission, subscribe with
 * the server's VAPID key and hand the subscription to the server. If the final
 * hand-off fails, the local subscription is dropped again — the browser never
 * keeps a subscription the server will not send to.
 */
export async function subscribePush(): Promise<void> {
  if ((await getPushState()) === "unsupported") {
    throw new Error("This browser does not support web push notifications.");
  }

  let registration: ServiceWorkerRegistration;
  try {
    await navigator.serviceWorker.register(SW_PATH);
    registration = await navigator.serviceWorker.ready; // subscribing needs an *active* worker
  } catch (err) {
    throw new Error(`Could not install the notification service worker (${detail(err)}).`);
  }

  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error(
      "Notification permission was not granted — allow notifications for this site in the browser settings, then retry.",
    );
  }

  let applicationServerKey: Uint8Array<ArrayBuffer>;
  try {
    applicationServerKey = urlBase64ToUint8Array((await api.vapidKey()).key);
  } catch (err) {
    throw new Error(`Could not fetch the server's push key (${detail(err)}).`);
  }

  let subscription: PushSubscription;
  try {
    subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey,
    });
  } catch (err) {
    throw new Error(`The browser refused the push subscription (${detail(err)}).`);
  }

  try {
    await api.pushSubscribe(subscription.toJSON());
  } catch (err) {
    await subscription.unsubscribe().catch(() => undefined);
    throw new Error(`Could not register the subscription with the server (${detail(err)}).`);
  }
}

/**
 * Tell the server to stop sending, then drop the browser-side subscription.
 * Server first: if it fails, the subscription stays and the user can retry.
 * Already unsubscribed is not an error — there is nothing to undo.
 */
export async function unsubscribePush(): Promise<void> {
  if (!("serviceWorker" in navigator)) {
    throw new Error("This browser does not support web push notifications.");
  }
  const registration = await navigator.serviceWorker.getRegistration();
  const subscription = (await registration?.pushManager.getSubscription()) ?? null;
  if (subscription === null) return;

  try {
    await api.pushUnsubscribe(subscription.endpoint);
  } catch (err) {
    throw new Error(`Could not remove the subscription from the server (${detail(err)}).`);
  }
  try {
    await subscription.unsubscribe();
  } catch (err) {
    throw new Error(
      `The server stopped sending, but the browser kept its subscription (${detail(err)}) — retry, or clear this site's data.`,
    );
  }
}
