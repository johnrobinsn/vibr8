// @vitest-environment jsdom
//
// Guards the "spinner forever" regression: if the desktop WebRTC
// offer fails synchronously (server 500, network error, etc.), the
// promise thrown by api.webrtcOffer used to bubble out and leave
// desktopStatus stuck at "connecting" — the UI kept rendering the
// spinner overlay forever with no error state. The fix wraps the
// signaling block in try/catch and hands off to
// _handleDesktopDisconnect, which retries a few times and then flips
// desktopStatus to "offline" so the DesktopView "Node offline" card
// (with a Retry button) is visible.
//
// This test constructs the failure and confirms status leaves
// "connecting" and lands at "offline" within the retry budget.

import { vi, describe, it, expect, beforeEach, afterEach } from "vitest";

// jsdom lacks matchMedia + WebRTC. Stub before store/webrtc import.
vi.hoisted(() => {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });

  // Minimal RTCPeerConnection: enough surface for _connectDesktop to
  // reach the api.webrtcOffer call. All promise-returning methods
  // resolve; the callbacks (ontrack, onconnectionstatechange, etc.)
  // are never fired here — the offer failure preempts them.
  class FakeRTCPeerConnection {
    localDescription: { sdp: string; type: string } | null = null;
    iceGatheringState = "complete";
    onconnectionstatechange: (() => void) | null = null;
    ontrack: ((ev: unknown) => void) | null = null;
    onicegatheringstatechange: (() => void) | null = null;
    connectionState = "new";

    createOffer() {
      return Promise.resolve({ sdp: "fake-sdp", type: "offer" });
    }
    setLocalDescription(desc: { sdp: string; type: string }) {
      this.localDescription = desc;
      return Promise.resolve();
    }
    setRemoteDescription(_desc: unknown) {
      return Promise.resolve();
    }
    createDataChannel(_name: string) {
      return {
        readyState: "open",
        addEventListener: () => {},
        send: () => {},
        onmessage: null,
      };
    }
    addTransceiver(_kind: string, _init: unknown) {
      return {};
    }
    close() {}
  }
  (window as unknown as Record<string, unknown>).RTCPeerConnection = FakeRTCPeerConnection;
  (window as unknown as Record<string, unknown>).RTCSessionDescription = class {
    constructor(public init: { sdp: string; type: string }) {}
  };
  (window as unknown as Record<string, unknown>).MediaStream = class {
    constructor(public tracks: unknown[] = []) {}
  };
});

vi.mock("./utils/names.js", () => ({
  generateUniqueSessionName: vi.fn(() => "Test Session"),
}));

// Stub api.webrtcOffer to reject; also stub getIceServers so
// _connectDesktop's preflight doesn't fail on jsdom's lack of fetch.
vi.mock("./api.js", () => ({
  api: {
    webrtcOffer: vi.fn().mockRejectedValue(new Error("HTTP 500: NoDisplayError")),
    getIceServers: vi.fn().mockResolvedValue({ iceServers: [] }),
  },
}));

let webrtc: typeof import("./webrtc.js");
let useStore: typeof import("./store.js").useStore;

beforeEach(async () => {
  vi.resetModules();
  vi.useFakeTimers();
  webrtc = await import("./webrtc.js");
  useStore = (await import("./store.js")).useStore;
  useStore.setState({
    desktopStatus: "idle",
    activeNodeId: "test-node",
    clientId: "test-client",
    tabId: "test-tab",
  } as any);
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("startDesktopStream — signaling failure", () => {
  it("does not leave desktopStatus stuck at 'connecting' after api.webrtcOffer rejects", async () => {
    // Fire off the start. It will reject in the background (offer
    // throws), we care about the resulting store state.
    void webrtc.startWebRTC({ desktop: true }).catch(() => {});

    // Let the initial connect attempt (microtasks + the rejected
    // fetch) settle.
    for (let i = 0; i < 5; i++) await Promise.resolve();

    // At this point _connectDesktop has thrown and called
    // _handleDesktopDisconnect. Status must have moved off
    // "connecting" — either to "reconnecting" (mid-retry) or
    // "offline" (retries exhausted).
    const status = useStore.getState().desktopStatus;
    expect(status).not.toBe("connecting");
  });

  it("lands on 'offline' once the retry budget is exhausted", async () => {
    void webrtc.startWebRTC({ desktop: true }).catch(() => {});

    // Reconnect delays are 1s + 2s + 4s (see DESKTOP_RECONNECT_DELAYS
    // in webrtc.ts). Advance timers enough to fire all three
    // rescheduled attempts, then let microtasks drain each time.
    for (let i = 0; i < 10; i++) {
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(1000);
    }

    expect(useStore.getState().desktopStatus).toBe("offline");
    // Stream flag is cleared so DesktopView doesn't render a
    // stale video frame under the offline card.
    expect(useStore.getState().desktopStreamActive).toBe(false);
  });
});
