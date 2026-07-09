import { api } from "./api";

/** Connection states surfaced to the UI via {@link WhepPlayer.onstate}. */
export type PlayerState = "connecting" | "live" | "failed";

/** How long we wait for ICE gathering before sending the offer anyway. */
const ICE_GATHER_TIMEOUT_MS = 2000;

/**
 * Resolves when ICE gathering completes, or after a short timeout — WHEP servers
 * are typically host-reachable, so a partial candidate set is good enough.
 */
function waitForIceGathering(pc: RTCPeerConnection, timeoutMs: number): Promise<void> {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    let timer = 0;
    const finish = (): void => {
      pc.removeEventListener("icegatheringstatechange", onChange);
      window.clearTimeout(timer);
      resolve();
    };
    const onChange = (): void => {
      if (pc.iceGatheringState === "complete") finish();
    };
    timer = window.setTimeout(finish, timeoutMs);
    pc.addEventListener("icegatheringstatechange", onChange);
  });
}

/**
 * Minimal WHEP (WebRTC-HTTP Egress Protocol) player.
 *
 * Lifecycle: construct with a `<video>` element and a camera id, assign
 * `onstate` if you want state updates, then `start()`. Call `stop()` to tear
 * down — it closes the peer connection and detaches the media stream, and is
 * safe to call at any point, including while `start()` is still negotiating.
 *
 * `start()` never rejects: failures (offer/answer errors, unreachable server,
 * dropped connection) are reported as `onstate("failed")` so callers have a
 * single channel to react on — e.g. by falling back to snapshots.
 */
export class WhepPlayer {
  /** Optional observer for connection state changes. */
  onstate?: (s: PlayerState) => void;

  private pc: RTCPeerConnection | null = null;
  private stopped = false;

  constructor(
    private readonly video: HTMLVideoElement,
    private readonly camera: string,
  ) {}

  async start(): Promise<void> {
    if (this.stopped || this.pc) return; // already stopped or already started
    this.emit("connecting");

    const pc = new RTCPeerConnection({});
    this.pc = pc;

    pc.addTransceiver("video", { direction: "recvonly" });
    pc.addTransceiver("audio", { direction: "recvonly" });

    pc.ontrack = (ev: RTCTrackEvent) => {
      if (this.stopped) return;
      const stream = ev.streams[0] ?? new MediaStream([ev.track]);
      this.video.muted = true;
      this.video.autoplay = true;
      this.video.playsInline = true;
      if (this.video.srcObject !== stream) this.video.srcObject = stream;
      void this.video.play().catch(() => {
        // Autoplay can be rejected before the user interacts with the page;
        // muted playback is normally allowed, so this is best-effort.
      });
    };

    pc.onconnectionstatechange = () => {
      if (this.stopped) return;
      switch (pc.connectionState) {
        case "connected":
          this.emit("live");
          break;
        case "failed":
        case "disconnected":
        case "closed":
          this.emit("failed");
          break;
        default:
          break;
      }
    };

    try {
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await waitForIceGathering(pc, ICE_GATHER_TIMEOUT_MS);
      if (this.stopped) return;

      const localSdp = pc.localDescription?.sdp;
      if (!localSdp) throw new Error("no local SDP available after ICE gathering");

      const answerSdp = await api.whep(this.camera, localSdp);
      if (this.stopped) return;
      await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });
    } catch (err) {
      if (!this.stopped) {
        console.warn(`WHEP negotiation failed for camera "${this.camera}"`, err);
        this.emit("failed");
      }
    }
  }

  stop(): void {
    this.stopped = true;
    if (this.pc) {
      this.pc.ontrack = null;
      this.pc.onconnectionstatechange = null;
      this.pc.close();
      this.pc = null;
    }
    this.video.srcObject = null;
  }

  private emit(s: PlayerState): void {
    this.onstate?.(s);
  }
}

/** Codec preference list sent to go2rtc's MSE endpoint (its own client sends the same). */
const MSE_CODECS =
  "avc1.640029,avc1.64001F,avc1.4D401F,avc1.42E01F," +
  "hvc1.1.6.L153.B0,hev1.1.6.L153.B0,mp4a.40.2,mp4a.40.5,flac,opus";

/** Keep at most this many seconds buffered behind the live edge before trimming. */
const MSE_KEEP_BEHIND_S = 15;
const MSE_TRIM_AT_S = 30;

/**
 * How long MSE gets to actually *play* before giving up. A WebSocket that opens but never
 * delivers media (camera asleep / not publishing) would otherwise sit in "connecting"
 * forever — the tile must fall back to snapshots and show the server's diagnosis instead.
 * Generous on purpose: a healthy stream may take several seconds to ship its first keyframe.
 */
const MSE_PLAYING_DEADLINE_MS = 15000;

/**
 * MSE player: fMP4 over an authenticated same-origin WebSocket
 * (`/api/v1/streams/{camera}/mse`, relayed by the server to go2rtc).
 *
 * This is the transport that works in *every* topology — no ICE, no candidates,
 * plain TCP through the same origin the app came from. WebRTC (lower latency)
 * is attempted first by {@link LivePlayer}; MSE is the reliable fallback.
 */
export class MsePlayer {
  onstate?: (s: PlayerState) => void;

  private ws: WebSocket | null = null;
  private ms: MediaSource | null = null;
  private sb: SourceBuffer | null = null;
  private queue: ArrayBuffer[] = [];
  private objectUrl: string | null = null;
  private stopped = false;
  private sawLive = false;
  private deadline = 0;
  private readonly onPlaying = (): void => {
    if (!this.stopped && !this.sawLive) {
      this.sawLive = true;
      window.clearTimeout(this.deadline);
      this.emit("live");
    }
  };

  constructor(
    private readonly video: HTMLVideoElement,
    private readonly camera: string,
  ) {}

  start(): void {
    if (this.stopped || this.ws) return;
    this.emit("connecting");
    this.deadline = window.setTimeout(() => {
      if (!this.stopped && !this.sawLive) this.fail();
    }, MSE_PLAYING_DEADLINE_MS);
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const url = `${scheme}://${location.host}/api/v1/streams/${encodeURIComponent(this.camera)}/mse`;
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => {
      ws.send(JSON.stringify({ type: "mse", value: MSE_CODECS }));
    };
    ws.onmessage = (ev: MessageEvent) => {
      if (this.stopped) return;
      if (typeof ev.data === "string") {
        try {
          const msg = JSON.parse(ev.data) as { type?: string; value?: string };
          if (msg.type === "mse" && msg.value) this.setupMediaSource(msg.value);
        } catch {
          // Non-JSON text frames are protocol noise — ignore.
        }
        return;
      }
      this.queue.push(ev.data as ArrayBuffer);
      this.flush();
    };
    ws.onerror = () => this.fail();
    ws.onclose = () => {
      if (!this.stopped) this.fail();
    };
  }

  stop(): void {
    this.stopped = true;
    window.clearTimeout(this.deadline);
    this.video.removeEventListener("playing", this.onPlaying);
    if (this.ws) {
      this.ws.onmessage = null;
      this.ws.onerror = null;
      this.ws.onclose = null;
      this.ws.close();
      this.ws = null;
    }
    if (this.objectUrl) {
      URL.revokeObjectURL(this.objectUrl);
      this.objectUrl = null;
    }
    this.queue = [];
    this.sb = null;
    this.ms = null;
    if (this.video.src.startsWith("blob:")) {
      this.video.removeAttribute("src");
      this.video.load();
    }
  }

  private setupMediaSource(mimeValue: string): void {
    const mime = mimeValue.includes("codecs=") ? mimeValue : `video/mp4; codecs="${mimeValue}"`;
    if (!("MediaSource" in window) || !MediaSource.isTypeSupported(mime)) {
      console.warn(`MSE: unsupported type "${mime}" for camera "${this.camera}"`);
      this.fail();
      return;
    }
    const ms = new MediaSource();
    this.ms = ms;
    this.objectUrl = URL.createObjectURL(ms);
    this.video.muted = true;
    this.video.autoplay = true;
    this.video.playsInline = true;
    this.video.addEventListener("playing", this.onPlaying);
    this.video.src = this.objectUrl;
    ms.addEventListener("sourceopen", () => {
      if (this.stopped || this.ms !== ms) return;
      try {
        const sb = ms.addSourceBuffer(mime);
        sb.mode = "segments";
        sb.addEventListener("updateend", () => this.flush());
        this.sb = sb;
        this.flush();
      } catch (err) {
        console.warn(`MSE: addSourceBuffer failed for camera "${this.camera}"`, err);
        this.fail();
      }
    });
    void this.video.play().catch(() => {
      // Muted autoplay is normally allowed; best-effort.
    });
  }

  private flush(): void {
    const { sb, ms } = this;
    if (!sb || !ms || ms.readyState !== "open" || sb.updating) return;
    const next = this.queue.shift();
    if (next) {
      try {
        sb.appendBuffer(next);
      } catch (err) {
        // QuotaExceeded or a torn-down buffer: trim and let the next frame retry once.
        console.warn(`MSE: append failed for camera "${this.camera}"`, err);
        this.trim(true);
      }
      return;
    }
    this.trim(false);
    this.chaseLiveEdge();
  }

  private trim(aggressive: boolean): void {
    const { sb, video } = this;
    if (!sb || sb.updating) return;
    const buffered = video.buffered;
    if (buffered.length === 0) return;
    const start = buffered.start(0);
    const end = buffered.end(buffered.length - 1);
    const behind = aggressive ? MSE_KEEP_BEHIND_S / 3 : MSE_KEEP_BEHIND_S;
    if (end - start > MSE_TRIM_AT_S) {
      try {
        sb.remove(start, end - behind);
      } catch {
        // A racing updateend can invalidate the window; the next flush retries.
      }
    }
  }

  private chaseLiveEdge(): void {
    const { video } = this;
    const buffered = video.buffered;
    if (buffered.length === 0) return;
    const end = buffered.end(buffered.length - 1);
    // If playback drifted far behind (tab was hidden, slow start), jump near the edge.
    if (end - video.currentTime > 5) {
      video.currentTime = Math.max(buffered.start(buffered.length - 1), end - 0.5);
    }
  }

  private fail(): void {
    if (this.stopped) return;
    this.stop();
    this.stopped = false; // allow the owner to retry via start() if it wants
    this.emit("failed");
  }

  private emit(s: PlayerState): void {
    this.onstate?.(s);
  }
}

/** How long WebRTC gets to reach `live` before the player falls back to MSE. */
const WHEP_DEADLINE_MS = 4500;

/**
 * The live player the UI uses: WebRTC first (sub-second when ICE works, e.g. with
 * `server.webrtc_candidates` configured), transparent fallback to MSE (works in every
 * topology), `failed` only when both transports are exhausted — at which point the
 * tile switches to refreshing snapshots.
 */
export class LivePlayer {
  onstate?: (s: PlayerState) => void;
  /** Which transport delivered `live` — surfaced in the tile's status label. */
  transport: "webrtc" | "mse" | null = null;
  /** Last emitted state — lets a re-attaching tile (keep-alive pool) sync immediately. */
  state: PlayerState = "connecting";

  private whep: WhepPlayer | null = null;
  private mse: MsePlayer | null = null;
  private deadline = 0;
  private stopped = false;
  private fellBack = false;

  constructor(
    private readonly video: HTMLVideoElement,
    private readonly camera: string,
  ) {}

  start(): void {
    if (this.stopped || this.whep || this.mse) return;
    this.emit("connecting");
    const whep = new WhepPlayer(this.video, this.camera);
    this.whep = whep;
    whep.onstate = (s) => {
      if (this.stopped || this.fellBack) return;
      if (s === "live") {
        this.transport = "webrtc";
        window.clearTimeout(this.deadline);
        this.emit("live");
      } else if (s === "failed") {
        this.fallbackToMse();
      }
    };
    this.deadline = window.setTimeout(() => {
      if (!this.stopped && this.transport === null) this.fallbackToMse();
    }, WHEP_DEADLINE_MS);
    void whep.start();
  }

  stop(): void {
    this.stopped = true;
    window.clearTimeout(this.deadline);
    this.whep?.stop();
    this.whep = null;
    this.mse?.stop();
    this.mse = null;
  }

  private fallbackToMse(): void {
    if (this.fellBack || this.stopped) return;
    this.fellBack = true;
    window.clearTimeout(this.deadline);
    this.whep?.stop();
    this.whep = null;
    const mse = new MsePlayer(this.video, this.camera);
    this.mse = mse;
    mse.onstate = (s) => {
      if (this.stopped) return;
      if (s === "live") {
        this.transport = "mse";
        this.emit("live");
      } else if (s === "failed") {
        this.emit("failed");
      }
    };
    mse.start();
  }

  private emit(s: PlayerState): void {
    this.state = s;
    this.onstate?.(s);
  }
}

/**
 * Keep-alive pool: releasing a player parks it (still connected) for a grace period so
 * navigating away and back re-attaches instantly instead of renegotiating — the
 * "always-open stream" experience without holding every camera open forever.
 */
const POOL_KEEPALIVE_MS = 60_000;

type PooledPlayer = { player: LivePlayer; video: HTMLVideoElement; timer: number };

const pool = new Map<string, PooledPlayer>();

export function acquireLivePlayer(camera: string): { player: LivePlayer; video: HTMLVideoElement } {
  const held = pool.get(camera);
  if (held) {
    pool.delete(camera);
    window.clearTimeout(held.timer);
    return { player: held.player, video: held.video };
  }
  const video = document.createElement("video");
  video.muted = true;
  video.autoplay = true;
  video.playsInline = true;
  return { player: new LivePlayer(video, camera), video };
}

export function releaseLivePlayer(
  camera: string,
  player: LivePlayer,
  video: HTMLVideoElement,
): void {
  const existing = pool.get(camera);
  if (existing) {
    window.clearTimeout(existing.timer);
    existing.player.stop();
  }
  const timer = window.setTimeout(() => {
    pool.delete(camera);
    player.stop();
  }, POOL_KEEPALIVE_MS);
  pool.set(camera, { player, video, timer });
}
