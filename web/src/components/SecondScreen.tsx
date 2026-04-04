import { useState, useEffect, useLayoutEffect, useCallback, useRef } from "react";
import { useStore } from "../store.js";
import { api } from "../api.js";
import { MessageFeed } from "./MessageFeed.js";
import { MarkdownContent } from "./MessageBubble.js";
import { handleMessage } from "../ws.js";
import { connectDesktopViewer, disconnectDesktopViewer, sendDesktopViewerInput, getViewerClipboard, setViewerClipboard } from "../webrtc.js";

const RING0_SESSION_ID = "ring0";

/** Second screen gets its own persistent client ID, separate from the primary tab. */
function getSecondScreenClientId(): string {
  const key = "vibr8_ss_client_id";
  let id = localStorage.getItem(key);
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem(key, id);
  }
  return id;
}

type PairingState = "checking" | "unpaired" | "paired";

export function SecondScreen() {
  const [clientId] = useState(getSecondScreenClientId);
  const [pairingState, setPairingState] = useState<PairingState>("checking");
  const [pairingCode, setPairingCode] = useState<string | null>(null);
  const [pairedUser, setPairedUser] = useState<string | null>(null);
  const [clientNameLocal, setClientNameLocal] = useState<string | null>(null);
  const clientNameFromStore = useStore((s) => s.secondScreenClientName);
  const [renameOpen, setRenameOpen] = useState(false);
  const pushedContent = useStore((s) => s.secondScreenContent);
  const mirroredSessionId = useStore((s) => s.mirroredSessionId);
  const sessionNames = useStore((s) => s.sessionNames);
  const scale = useStore((s) => s.secondScreenScale);
  const tvSafe = useStore((s) => s.secondScreenTvSafe);
  const secondScreenDarkMode = useStore((s) => s.secondScreenDarkMode);

  // Set client role to secondscreen on mount
  useEffect(() => {
    useStore.getState().setClientRole("secondscreen");
    return () => {
      useStore.getState().setClientRole("primary");
    };
  }, []);

  // Apply second screen dark mode before paint (independent of primary dark mode)
  useLayoutEffect(() => {
    document.documentElement.classList.toggle("dark", secondScreenDarkMode);
    return () => {
      // Restore primary dark mode on unmount
      const isDark = useStore.getState().darkMode;
      document.documentElement.classList.toggle("dark", isDark);
    };
  }, [secondScreenDarkMode]);

  // Report device info on mount (fire-and-forget)
  useEffect(() => {
    api.reportDeviceInfo(clientId, {
      screenWidth: window.innerWidth,
      screenHeight: window.innerHeight,
      devicePixelRatio: window.devicePixelRatio,
      userAgent: navigator.userAgent,
      platform: navigator.platform,
      language: navigator.language,
      touchSupport: navigator.maxTouchPoints > 0,
    }).catch(() => {});
  }, [clientId]);

  // Fetch client name on mount
  useEffect(() => {
    api.getClientMetadata(clientId).then((meta) => {
      if (meta.name) setClientNameLocal(meta.name);
    }).catch(() => {});
  }, [clientId]);

  const clientName = clientNameFromStore ?? clientNameLocal;
  const displayClientName = clientName || clientId.slice(0, 8) + "…";

  const handleRename = useCallback(async (newName: string) => {
    await api.updateClientMetadata(clientId, { name: newName });
    setClientNameLocal(newName || null);
    useStore.getState().setSecondScreenClientName(newName || null);
    setRenameOpen(false);
  }, [clientId]);

  // Check pairing status on mount and generate code if unpaired (single flow)
  useEffect(() => {
    let cancelled = false;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    async function checkAndGenerate() {
      try {
        const status = await api.secondScreenStatus(clientId);
        if (cancelled) return;
        if (status.paired && status.role === "secondscreen" && status.pairedUser) {
          setPairingState("paired");
          setPairedUser(status.pairedUser);
          return;
        }
      } catch {
        if (cancelled) return;
      }
      setPairingState("unpaired");
      try {
        const { code } = await api.secondScreenPairCode(clientId);
        if (!cancelled) setPairingCode(code);
      } catch (err) {
        console.error("[second-screen] Failed to generate code, retrying in 3s:", err);
        if (!cancelled) retryTimer = setTimeout(checkAndGenerate, 3000);
      }
    }

    checkAndGenerate();
    return () => { cancelled = true; if (retryTimer) clearTimeout(retryTimer); };
  }, [clientId]);

  // Keep a ref to handleMessage so the WS onmessage always dispatches to the
  // latest version (survives HMR replacing ws.ts / store.ts modules).
  const handleMsgRef = useRef(handleMessage);
  handleMsgRef.current = handleMessage;

  // When paired, open a dedicated WebSocket to Ring0 (control channel).
  // The connectKey is bumped to force a reconnect when messages are lost
  // (e.g. after HMR replaces the store with an empty messages map).
  const wsRef = useRef<WebSocket | null>(null);
  const [connectKey, setConnectKey] = useState(0);
  useEffect(() => {
    if (pairingState !== "paired") return;
    useStore.getState().setCurrentSession(RING0_SESSION_ID);

    let alive = true;
    let keepalive: ReturnType<typeof setInterval> | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    function connect() {
      if (!alive) return;
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${proto}//${location.host}/ws/browser/${RING0_SESSION_ID}?clientId=${encodeURIComponent(clientId)}&role=secondscreen`;
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        useStore.getState().setConnectionStatus(RING0_SESSION_ID, "connected");
        keepalive = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping" }));
        }, 15000);
      };

      ws.onmessage = (event) => handleMsgRef.current(RING0_SESSION_ID, event, ws);

      ws.onclose = () => {
        if (keepalive) { clearInterval(keepalive); keepalive = null; }
        wsRef.current = null;
        useStore.getState().setConnectionStatus(RING0_SESSION_ID, "disconnected");
        // Auto-reconnect after 2s
        if (alive) reconnectTimer = setTimeout(connect, 2000);
      };

      ws.onerror = () => ws.close();
    }

    connect();

    return () => {
      alive = false;
      if (keepalive) clearInterval(keepalive);
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (wsRef.current) { wsRef.current.close(); wsRef.current = null; }
    };
  }, [pairingState, connectKey]);

  // After HMR, the store may have been recreated with an empty messages map
  // while the WS is still connected.  Detect this and force a reconnect so
  // the server re-sends session_init + message_history.  Use a delay to avoid
  // false positives on normal reconnects (messages arrive shortly after open).
  const ring0Messages = useStore((s) => s.messages.get(RING0_SESSION_ID));
  const ring0Connected = useStore((s) => s.connectionStatus.get(RING0_SESSION_ID));
  useEffect(() => {
    if (pairingState === "paired" && ring0Connected === "connected" && (!ring0Messages || ring0Messages.length === 0)) {
      const timer = setTimeout(() => setConnectKey((k) => k + 1), 500);
      return () => clearTimeout(timer);
    }
  }, [pairingState, ring0Connected, ring0Messages]);

  // Mirror WebSocket — opens a second WS to the mirrored session for live data
  const mirrorWsRef = useRef<WebSocket | null>(null);
  useEffect(() => {
    if (!mirroredSessionId || mirroredSessionId === RING0_SESSION_ID) {
      // No mirror needed (Ring0 is already connected via the control channel)
      if (mirrorWsRef.current) { mirrorWsRef.current.close(); mirrorWsRef.current = null; }
      return;
    }

    let alive = true;
    let keepalive: ReturnType<typeof setInterval> | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    function connect() {
      if (!alive) return;
      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${proto}//${location.host}/ws/browser/${mirroredSessionId}?clientId=${encodeURIComponent(clientId)}&role=secondscreen&mirror=true`;
      const ws = new WebSocket(url);
      mirrorWsRef.current = ws;

      ws.onopen = () => {
        keepalive = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "ping" }));
        }, 15000);
      };

      ws.onmessage = (event) => handleMessage(mirroredSessionId!, event, ws);

      ws.onclose = () => {
        if (keepalive) { clearInterval(keepalive); keepalive = null; }
        mirrorWsRef.current = null;
        if (alive) reconnectTimer = setTimeout(connect, 2000);
      };

      ws.onerror = () => ws.close();
    }

    connect();

    return () => {
      alive = false;
      if (keepalive) clearInterval(keepalive);
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (mirrorWsRef.current) { mirrorWsRef.current.close(); mirrorWsRef.current = null; }
    };
  }, [mirroredSessionId, clientId]);

  // Generate a fresh pairing code (used after unpair)
  const generateCode = useCallback(async () => {
    try {
      const { code } = await api.secondScreenPairCode(clientId);
      setPairingCode(code);
    } catch (err) {
      console.error("[second-screen] Failed to generate code:", err);
    }
  }, [clientId]);

  // Listen for pairing completion via polling
  useEffect(() => {
    if (pairingState !== "unpaired") return;

    const interval = setInterval(async () => {
      try {
        const status = await api.secondScreenStatus(clientId);
        if (status.paired && status.role === "secondscreen") {
          setPairingState("paired");
          setPairedUser(status.pairedUser ?? null);
          setPairingCode(null);
        }
      } catch {
        // ignore
      }
    }, 3000);

    return () => clearInterval(interval);
  }, [pairingState, clientId]);

  const handleUnpair = useCallback(async () => {
    await api.secondScreenUnpair(clientId);
    setPairingState("unpaired");
    setPairedUser(null);
    setPairingCode(null);
    if (wsRef.current) { wsRef.current.close(); wsRef.current = null; }
    if (mirrorWsRef.current) { mirrorWsRef.current.close(); mirrorWsRef.current = null; }
    useStore.getState().setCurrentSession(null);
    useStore.getState().setMirroredSessionId(null);
    useStore.getState().setSecondScreenContent(null);
    generateCode();
  }, [clientId, generateCode]);

  const handleGoHome = useCallback(() => {
    useStore.getState().setMirroredSessionId(null);
    useStore.getState().setSecondScreenContent(null);
  }, []);

  if (pairingState === "checking") {
    return (
      <div className="h-[100dvh] flex items-center justify-center bg-cc-bg text-cc-fg">
        <div className="text-cc-fg-muted">Checking pairing status…</div>
      </div>
    );
  }

  if (pairingState === "unpaired") {
    return (
      <div className="h-[100dvh] flex items-center justify-center bg-cc-bg text-cc-fg">
        <div className="text-center space-y-8">
          <div className="space-y-2">
            <h1 className="text-2xl font-semibold">Second Screen</h1>
            <p className="text-cc-fg-muted text-sm">Enter this code on your primary device to pair</p>
          </div>

          {pairingCode ? (
            <div className="font-mono text-6xl tracking-[0.3em] font-bold text-cc-fg select-all">
              {pairingCode}
            </div>
          ) : (
            <div className="text-cc-fg-muted">Generating code…</div>
          )}

          <p className="text-cc-fg-muted text-xs max-w-sm mx-auto">
            On your primary device, use the command palette or tell Ring0:
            <br />
            <span className="text-cc-fg font-medium">"pair second screen {pairingCode}"</span>
          </p>
        </div>
      </div>
    );
  }

  // Determine what to show and the status bar label
  const isHome = !pushedContent && !mirroredSessionId;
  const isMirroring = !pushedContent && !!mirroredSessionId;
  const displaySessionId = mirroredSessionId || RING0_SESSION_ID;
  const displayName = isMirroring
    ? sessionNames.get(mirroredSessionId!) || mirroredSessionId!
    : "Ring0";

  // Paired — show content
  return (
    <div className="h-[100dvh] flex flex-col bg-cc-bg text-cc-fg" style={tvSafe > 0 ? { padding: `${tvSafe}%` } : undefined}>
      {/* Status bar */}
      <div className="shrink-0 flex items-center justify-between px-4 py-2 border-b border-cc-border text-xs text-cc-fg-muted">
        <div className="flex items-center gap-2">
          <span className="inline-block w-2 h-2 rounded-full bg-green-500" />
          <button
            onClick={() => setRenameOpen(true)}
            className="hover:text-cc-fg transition-colors underline decoration-dotted underline-offset-2"
          >
            {displayClientName}
          </button>
          <span className="text-cc-fg-muted">
            {pushedContent
              ? `— ${pushedContent.type}`
              : isMirroring
                ? `— Mirroring: ${displayName}`
                : "— Ring0"}
          </span>
        </div>
        <div className="flex items-center gap-3">
          {!isHome && (
            <button
              onClick={handleGoHome}
              className="text-cc-fg-muted hover:text-cc-fg transition-colors"
            >
              Home
            </button>
          )}
          <button
            onClick={handleUnpair}
            className="text-cc-fg-muted hover:text-cc-fg transition-colors"
          >
            Unpair
          </button>
        </div>
      </div>

      {/* Rename dialog */}
      {renameOpen && (
        <RenameDialog
          currentName={clientName || ""}
          onSave={handleRename}
          onCancel={() => setRenameOpen(false)}
        />
      )}

      {/* Content area — desktop viewer renders outside zoom for correct coordinate mapping */}
      {pushedContent?.type === "desktop" ? (
        <div className="flex-1 min-h-0">
          <DesktopViewer onHome={handleGoHome} nodeId={pushedContent.nodeId} />
        </div>
      ) : (
        <div className="flex-1 flex flex-col min-h-0" style={{ zoom: scale }}>
          {pushedContent ? (
            <PushedContentView content={pushedContent} onHome={handleGoHome} />
          ) : (
            <MessageFeed sessionId={displaySessionId} />
          )}
        </div>
      )}
    </div>
  );
}

function RenameDialog({
  currentName,
  onSave,
  onCancel,
}: {
  currentName: string;
  onSave: (name: string) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(currentName);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onSave(name.trim());
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={onCancel}>
      <form
        onSubmit={handleSubmit}
        onClick={(e) => e.stopPropagation()}
        className="bg-cc-bg border border-cc-border rounded-lg p-4 shadow-xl w-72 space-y-3"
      >
        <label className="block text-sm font-medium text-cc-fg">Device Name</label>
        <input
          ref={inputRef}
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Escape") onCancel(); }}
          placeholder="e.g. Tesla, Phone, iPad"
          className="w-full px-3 py-2 rounded border border-cc-border bg-cc-input-bg text-cc-fg text-sm focus:outline-none focus:ring-1 focus:ring-cc-primary/50"
        />
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="px-3 py-1.5 text-xs text-cc-fg-muted hover:text-cc-fg transition-colors"
          >
            Cancel
          </button>
          <button
            type="submit"
            className="px-3 py-1.5 text-xs bg-cc-primary text-white rounded hover:bg-cc-primary-hover transition-colors"
          >
            Save
          </button>
        </div>
      </form>
    </div>
  );
}

function BackButton({ onClick, label = "Home" }: { onClick: () => void; label?: string }) {
  return (
    <div className="text-center mt-4 pb-4">
      <button onClick={onClick} className="text-xs text-cc-fg-muted hover:text-cc-fg transition-colors">
        {label}
      </button>
    </div>
  );
}

function PushedContentView({
  content,
  onHome,
}: {
  content: { type: string; content: string; filename?: string; nodeId?: string };
  onHome: () => void;
}) {
  const [imgError, setImgError] = useState(false);
  const [imgLoaded, setImgLoaded] = useState(false);

  // Reset image state when content changes
  useEffect(() => {
    setImgError(false);
    setImgLoaded(false);
  }, [content.content]);

  // Desktop remote stream viewer
  if (content.type === "desktop") {
    return <DesktopViewer onHome={onHome} nodeId={content.nodeId} />;
  }

  // Image viewer
  if (content.type === "image") {
    let url = content.content.trim();
    // Auto-wrap bare base64 data (Ring0 may put base64 in content instead of image_data)
    if (url && !url.startsWith("data:") && !url.startsWith("http")) {
      url = `data:image/png;base64,${url}`;
    }
    return (
      <div className="h-full flex flex-col items-center justify-center p-8">
        {!imgLoaded && !imgError && (
          <div className="text-cc-fg-muted text-sm mb-4">Loading image…</div>
        )}
        {imgError ? (
          <div className="text-red-400 text-sm">
            Failed to load image
            <div className="text-xs text-cc-fg-muted mt-1 break-all max-w-lg">{url}</div>
          </div>
        ) : (
          <img
            src={url}
            alt=""
            className="max-w-full max-h-[85vh] object-contain rounded"
            onLoad={() => setImgLoaded(true)}
            onError={() => setImgError(true)}
          />
        )}
        <BackButton onClick={onHome} />
      </div>
    );
  }

  // Markdown viewer
  if (content.type === "markdown") {
    return (
      <div className="h-full overflow-auto p-8">
        <div className="max-w-3xl mx-auto">
          <MarkdownContent text={content.content} />
        </div>
        <BackButton onClick={onHome} />
      </div>
    );
  }

  // File/text viewer
  if (content.type === "file") {
    return (
      <div className="h-full flex flex-col">
        {content.filename && (
          <div className="shrink-0 px-4 py-2 bg-cc-code-bg border-b border-cc-border font-mono text-sm text-cc-fg-muted">
            {content.filename}
          </div>
        )}
        <pre className="flex-1 overflow-auto p-4 text-sm font-mono leading-relaxed bg-cc-code-bg text-cc-code-fg whitespace-pre-wrap">
          {content.content}
        </pre>
        <BackButton onClick={onHome} />
      </div>
    );
  }

  // PDF viewer
  if (content.type === "pdf") {
    return (
      <div className="h-full flex flex-col">
        <iframe
          src={content.content}
          className="flex-1 w-full border-0"
          title="PDF Viewer"
        />
        <BackButton onClick={onHome} />
      </div>
    );
  }

  // HTML viewer (sandboxed)
  if (content.type === "html") {
    return (
      <div className="h-full flex flex-col">
        <iframe
          srcDoc={content.content}
          className="flex-1 w-full border-0 bg-white"
          title="HTML Content"
          sandbox="allow-scripts allow-same-origin"
        />
        <BackButton onClick={onHome} />
      </div>
    );
  }

  // Fallback: plain text
  return (
    <div className="h-full overflow-auto p-8">
      <div className="max-w-3xl mx-auto">
        <pre className="whitespace-pre-wrap text-sm leading-relaxed">{content.content}</pre>
      </div>
      <BackButton onClick={onHome} />
    </div>
  );
}

/** Full desktop controller for second screens — video + input + toolbar. */
function DesktopViewer({ onHome, nodeId }: { onHome: () => void; nodeId?: string }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<"connecting" | "connected" | "error">("connecting");
  const [error, setError] = useState<string | null>(null);
  const viewerIdRef = useRef(`ss-viewer-${crypto.randomUUID()}`);
  const lastMoveRef = useRef(0);

  const webrtcSupported = typeof RTCPeerConnection !== "undefined";

  // ── Toolbar state ──────────────────────────────────────────────────────
  const [showToolbar, setShowToolbar] = useState(true);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [vkbActive, setVkbActive] = useState(false);
  const vkbInputRef = useRef<HTMLInputElement>(null);
  const hideTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const [clipToast, setClipToast] = useState<string | null>(null);
  const clipToastTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const resetHideTimer = useCallback(() => {
    setShowToolbar(true);
    clearTimeout(hideTimer.current);
    hideTimer.current = setTimeout(() => setShowToolbar(false), 3000);
  }, []);

  // ── Connect WebRTC ─────────────────────────────────────────────────────
  useEffect(() => {
    if (!webrtcSupported) {
      setStatus("error");
      setError("WebRTC is not supported in this browser");
      return;
    }

    const viewerId = viewerIdRef.current;
    let cancelled = false;

    connectDesktopViewer(viewerId, nodeId)
      .then((stream) => {
        if (cancelled) { disconnectDesktopViewer(viewerId); return; }
        setStatus("connected");
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          videoRef.current.play().catch(() => {});
        }
      })
      .catch((err) => {
        if (!cancelled) { setStatus("error"); setError(err?.message || "Failed to connect"); }
      });

    return () => { cancelled = true; disconnectDesktopViewer(viewerId); };
  }, [webrtcSupported, nodeId]);

  // Fullscreen change listener
  useEffect(() => {
    const onChange = () => setIsFullscreen(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  // ── Coordinate mapping (object-contain, no zoom/pan) ───────────────────
  const getVideoCoords = useCallback(
    (clientX: number, clientY: number): { x: number; y: number } | null => {
      const video = videoRef.current;
      const container = containerRef.current;
      if (!video || !container || !video.videoWidth || !video.videoHeight) {
        console.warn("[desktop-viewer] getVideoCoords: no video dimensions", {
          video: !!video, container: !!container,
          videoWidth: video?.videoWidth, videoHeight: video?.videoHeight,
        });
        return null;
      }

      const cRect = container.getBoundingClientRect();
      const elW = cRect.width;
      const elH = cRect.height;
      const localX = clientX - cRect.left;
      const localY = clientY - cRect.top;

      // object-contain: compute letterbox/pillarbox
      const vidAR = video.videoWidth / video.videoHeight;
      const elAR = elW / elH;
      let renderW: number, renderH: number, renderX: number, renderY: number;
      if (vidAR > elAR) {
        renderW = elW; renderH = elW / vidAR; renderX = 0; renderY = (elH - renderH) / 2;
      } else {
        renderH = elH; renderW = elH * vidAR; renderY = 0; renderX = (elW - renderW) / 2;
      }

      return {
        x: Math.max(0, Math.min(1, (localX - renderX) / renderW)),
        y: Math.max(0, Math.min(1, (localY - renderY) / renderH)),
      };
    },
    [],
  );

  // Helper to send input on this viewer's data channel
  const sendCount = useRef(0);
  const send = useCallback((event: object) => {
    if (sendCount.current < 5) {
      console.log("[desktop-viewer] send", event, "viewerId=", viewerIdRef.current);
      sendCount.current++;
    }
    sendDesktopViewerInput(viewerIdRef.current, event);
  }, []);

  // ── Mouse handlers ─────────────────────────────────────────────────────
  const handleMouseMove = useCallback(
    (e: React.MouseEvent) => {
      const now = performance.now();
      if (now - lastMoveRef.current < 33) return;
      lastMoveRef.current = now;
      const coords = getVideoCoords(e.clientX, e.clientY);
      if (coords) send({ type: "mousemove", ...coords });
    },
    [getVideoCoords, send],
  );

  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      const coords = getVideoCoords(e.clientX, e.clientY);
      if (coords) send({ type: "mousedown", button: e.button, ...coords });
    },
    [getVideoCoords, send],
  );

  const handleMouseUp = useCallback(
    (e: React.MouseEvent) => {
      const coords = getVideoCoords(e.clientX, e.clientY);
      if (coords) send({ type: "mouseup", button: e.button, ...coords });
    },
    [getVideoCoords, send],
  );

  const handleContextMenu = useCallback((e: React.MouseEvent) => { e.preventDefault(); }, []);

  // ── Wheel (non-passive) ────────────────────────────────────────────────
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    const handler = (e: WheelEvent) => {
      e.preventDefault();
      const coords = getVideoCoords(e.clientX, e.clientY);
      if (coords) send({ type: "wheel", dx: e.deltaX, dy: e.deltaY, ...coords });
    };
    video.addEventListener("wheel", handler, { passive: false });
    return () => video.removeEventListener("wheel", handler);
  }, [getVideoCoords, send]);

  // ── Touch: single-finger → mouse, two-finger → ignored ────────────────
  const activeTouchCountRef = useRef(0);

  const handleTouchStart = useCallback(
    (e: React.TouchEvent) => {
      activeTouchCountRef.current = e.touches.length;
      if (e.touches.length === 1) {
        const t = e.changedTouches[0];
        if (!t) return;
        const coords = getVideoCoords(t.clientX, t.clientY);
        if (coords) send({ type: "mousedown", button: 0, ...coords });
      }
    },
    [getVideoCoords, send],
  );

  const handleTouchMove = useCallback(
    (e: React.TouchEvent) => {
      if (e.touches.length === 1 && activeTouchCountRef.current === 1) {
        const now = performance.now();
        if (now - lastMoveRef.current < 33) return;
        lastMoveRef.current = now;
        const t = e.changedTouches[0];
        if (!t) return;
        const coords = getVideoCoords(t.clientX, t.clientY);
        if (coords) send({ type: "mousemove", ...coords });
      }
    },
    [getVideoCoords, send],
  );

  const handleTouchEnd = useCallback(
    (e: React.TouchEvent) => {
      if (e.touches.length === 0 && activeTouchCountRef.current === 1) {
        const t = e.changedTouches[0];
        if (!t) return;
        const coords = getVideoCoords(t.clientX, t.clientY);
        if (coords) send({ type: "mouseup", button: 0, ...coords });
      }
      activeTouchCountRef.current = e.touches.length;
    },
    [getVideoCoords, send],
  );

  // ── Keyboard ───────────────────────────────────────────────────────────
  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.metaKey || (e.ctrlKey && e.key === "Tab")) return;
    e.preventDefault();
    send({ type: "keydown", key: e.key });
  }, [send]);

  const handleKeyUp = useCallback((e: React.KeyboardEvent) => {
    e.preventDefault();
    send({ type: "keyup", key: e.key });
  }, [send]);

  // ── Virtual keyboard ──────────────────────────────────────────────────
  const toggleVirtualKeyboard = useCallback(() => {
    if (vkbActive) {
      vkbInputRef.current?.blur();
    } else {
      setVkbActive(true);
      requestAnimationFrame(() => vkbInputRef.current?.focus());
    }
  }, [vkbActive]);

  const handleVkbKeyDown = useCallback((e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key.length > 1 || e.ctrlKey || e.altKey || e.metaKey) {
      e.preventDefault();
      send({ type: "keydown", key: e.key });
    }
  }, [send]);

  const handleVkbKeyUp = useCallback((e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key.length > 1 || e.ctrlKey || e.altKey || e.metaKey) {
      send({ type: "keyup", key: e.key });
    }
  }, [send]);

  const handleVkbInput = useCallback((e: React.FormEvent<HTMLInputElement>) => {
    const input = e.currentTarget;
    const data = (e.nativeEvent as InputEvent).data;
    if (data) {
      for (const char of data) { send({ type: "text", text: char }); }
    }
    input.value = "";
  }, [send]);

  const handleVkbBlur = useCallback(() => {
    setVkbActive(false);
    if (vkbInputRef.current) vkbInputRef.current.value = "";
  }, []);

  // ── Clipboard ──────────────────────────────────────────────────────────
  const showClipToast = useCallback((msg: string) => {
    setClipToast(msg);
    clearTimeout(clipToastTimer.current);
    clipToastTimer.current = setTimeout(() => setClipToast(null), 2000);
  }, []);

  const handleCopyFromRemote = useCallback(async () => {
    try {
      const text = await getViewerClipboard(viewerIdRef.current);
      await navigator.clipboard.writeText(text);
      showClipToast("Copied from remote");
    } catch { showClipToast("Copy failed"); }
  }, [showClipToast]);

  const handlePasteToRemote = useCallback(async () => {
    try {
      const text = await navigator.clipboard.readText();
      setViewerClipboard(viewerIdRef.current, text);
      showClipToast("Pasted to remote");
    } catch { showClipToast("Paste failed"); }
  }, [showClipToast]);

  // ── Toolbar actions ────────────────────────────────────────────────────
  const toggleFullscreen = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    if (document.fullscreenElement) { document.exitFullscreen(); }
    else { el.requestFullscreen(); }
  }, []);

  const handleDisconnect = useCallback(() => {
    disconnectDesktopViewer(viewerIdRef.current);
    onHome();
  }, [onHome]);

  // ── Error / not-supported states ───────────────────────────────────────
  if (!webrtcSupported) {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-4 p-8">
        <div className="text-4xl opacity-40">⚠</div>
        <div className="text-lg font-medium text-cc-fg-muted">WebRTC not supported</div>
        <div className="text-sm text-cc-fg-muted/70 text-center max-w-md">
          This browser does not support WebRTC, which is required for remote desktop streaming.
          Try using a recent version of Chrome, Firefox, Safari, or Edge.
        </div>
        <BackButton onClick={onHome} />
      </div>
    );
  }

  if (status === "error") {
    return (
      <div className="h-full flex flex-col items-center justify-center gap-4 p-8">
        <div className="text-4xl opacity-40">⚠</div>
        <div className="text-lg font-medium text-cc-fg-muted">Desktop stream unavailable</div>
        <div className="text-sm text-cc-fg-muted/70 text-center max-w-md">
          {error || "Could not connect to the remote desktop stream."}
        </div>
        <BackButton onClick={onHome} />
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="h-full w-full bg-black relative overflow-hidden select-none outline-none"
      tabIndex={0}
      onMouseMove={resetHideTimer}
      onMouseEnter={resetHideTimer}
      onKeyDown={handleKeyDown}
      onKeyUp={handleKeyUp}
    >
      {/* Video */}
      <video
        ref={videoRef}
        className="w-full h-full object-contain cursor-default"
        autoPlay
        playsInline
        muted
        onMouseMove={handleMouseMove}
        onMouseDown={handleMouseDown}
        onMouseUp={handleMouseUp}
        onContextMenu={handleContextMenu}
        onTouchStart={handleTouchStart}
        onTouchMove={handleTouchMove}
        onTouchEnd={handleTouchEnd}
      />

      {/* Hidden input for virtual keyboard */}
      <input
        ref={vkbInputRef}
        type="text"
        autoComplete="off"
        autoCorrect="off"
        autoCapitalize="off"
        spellCheck={false}
        className="absolute opacity-0 w-px h-px pointer-events-auto"
        style={{ top: 0, left: 0 }}
        onKeyDown={handleVkbKeyDown}
        onKeyUp={handleVkbKeyUp}
        onInput={handleVkbInput}
        onBlur={handleVkbBlur}
      />

      {/* Connecting overlay */}
      {status === "connecting" && (
        <div className="absolute inset-0 flex items-center justify-center bg-black/60 z-10">
          <div className="text-center space-y-2">
            <div className="w-6 h-6 border-2 border-white/30 border-t-white rounded-full animate-spin mx-auto" />
            <p className="text-white/70 text-sm">Connecting…</p>
          </div>
        </div>
      )}

      {/* Floating toolbar */}
      <div
        className={`absolute bottom-4 left-1/2 -translate-x-1/2 flex items-center gap-1 px-2 py-1.5 rounded-xl bg-black/70 backdrop-blur-sm border border-white/10 transition-opacity duration-300 z-20 ${
          showToolbar ? "opacity-100" : "opacity-0 pointer-events-none"
        }`}
      >
        {/* Fullscreen */}
        <SSToolbarButton
          title={isFullscreen ? "Exit fullscreen" : "Fullscreen"}
          onClick={toggleFullscreen}
        >
          {isFullscreen ? (
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-4 h-4">
              <path d="M5 2v3H2M11 2v3h3M5 14v-3H2M11 14v-3h3" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          ) : (
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-4 h-4">
              <path d="M2 6V2h4M14 6V2h-4M2 10v4h4M14 10v4h-4" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          )}
        </SSToolbarButton>

        {/* Virtual keyboard */}
        <SSToolbarButton
          title={vkbActive ? "Hide keyboard" : "Show keyboard"}
          onClick={toggleVirtualKeyboard}
          active={vkbActive}
        >
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" className="w-4 h-4">
            <rect x="1" y="4" width="14" height="9" rx="1.5" />
            <path d="M4 7h1M7 7h2M11 7h1M4 9.5h1M7 9.5h2M11 9.5h1M5 11.5h6" strokeLinecap="round" />
          </svg>
        </SSToolbarButton>

        {/* Clipboard: copy from remote */}
        <SSToolbarButton title="Copy from remote" onClick={handleCopyFromRemote}>
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" className="w-4 h-4">
            <rect x="5" y="1.5" width="8" height="10" rx="1.2" />
            <path d="M3 4.5H2.5a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h7a1 1 0 0 0 1-1V14" />
          </svg>
        </SSToolbarButton>

        {/* Clipboard: paste to remote */}
        <SSToolbarButton title="Paste to remote" onClick={handlePasteToRemote}>
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" className="w-4 h-4">
            <rect x="2.5" y="2.5" width="11" height="12" rx="1.2" />
            <path d="M5.5 1.5h5a1 1 0 0 1 1 1v1h-7v-1a1 1 0 0 1 1-1z" />
            <path d="M5.5 8h5M5.5 10.5h3" strokeLinecap="round" />
          </svg>
        </SSToolbarButton>

        {/* Divider */}
        <div className="w-px h-5 bg-white/20 mx-1" />

        {/* Disconnect / Home */}
        <SSToolbarButton title="Disconnect" onClick={handleDisconnect} danger>
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-4 h-4">
            <path d="M12 4L4 12M4 4l8 8" strokeLinecap="round" />
          </svg>
        </SSToolbarButton>
      </div>

      {/* Clipboard toast */}
      {clipToast && (
        <div className="absolute top-4 left-1/2 -translate-x-1/2 px-3 py-1.5 rounded-lg bg-black/80 text-white text-xs font-medium backdrop-blur-sm border border-white/10 z-20">
          {clipToast}
        </div>
      )}
    </div>
  );
}

function SSToolbarButton({
  children, onClick, title, danger, active,
}: {
  children: React.ReactNode; onClick: () => void; title: string;
  danger?: boolean; active?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      className={`flex items-center justify-center w-8 h-8 rounded-lg transition-colors cursor-pointer ${
        danger
          ? "text-white/70 hover:text-red-400 hover:bg-red-500/20"
          : active
          ? "text-blue-400 bg-blue-500/20"
          : "text-white/70 hover:text-white hover:bg-white/10"
      }`}
    >
      {children}
    </button>
  );
}
