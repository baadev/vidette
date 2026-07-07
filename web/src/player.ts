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
