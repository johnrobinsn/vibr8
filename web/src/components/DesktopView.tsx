import { useRef, useEffect, useState, useCallback } from "react";
import { useStore } from "../store.js";
import { stopDesktopStream, sendDesktopInput, retryDesktopStream, getRemoteClipboard, setRemoteClipboard } from "../webrtc.js";

type ScaleMode = "fit" | "fill";

export function DesktopView({ sessionId: _sessionId }: { sessionId: string }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const desktopStreamActive = useStore((s) => s.desktopStreamActive);
  const desktopRemoteStream = useStore((s) => s.desktopRemoteStream);
  const desktopStatus = useStore((s) => s.desktopStatus);
  const desktopStats = useStore((s) => s.desktopStats);
  const [scaleMode, setScaleMode] = useState<ScaleMode>("fit");
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [showToolbar, setShowToolbar] = useState(true);
  const [videoResolution, setVideoResolution] = useState<{ w: number; h: number } | null>(null);
  const hideTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  // ── Virtual keyboard state ──────────────────────────────────────────────
  const [vkbActive, setVkbActive] = useState(false);
  const vkbInputRef = useRef<HTMLInputElement>(null);

  // ── Clipboard toast ─────────────────────────────────────────────────────
  const [clipToast, setClipToast] = useState<string | null>(null);
  const clipToastTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const showClipToast = useCallback((msg: string) => {
    setClipToast(msg);
    clearTimeout(clipToastTimer.current);
    clipToastTimer.current = setTimeout(() => setClipToast(null), 2000);
  }, []);

  const handleCopyFromRemote = useCallback(async () => {
    try {
      const text = await getRemoteClipboard();
      await navigator.clipboard.writeText(text);
      showClipToast("Copied from remote");
    } catch {
      showClipToast("Copy failed");
    }
  }, [showClipToast]);

  const handlePasteToRemote = useCallback(async () => {
    try {
      const text = await navigator.clipboard.readText();
      setRemoteClipboard(text);
      showClipToast("Pasted to remote");
    } catch {
      showClipToast("Paste failed");
    }
  }, [showClipToast]);

  // ── Pinch-to-zoom state ────────────────────────────────────────────────
  const [zoom, setZoom] = useState(1);
  const [panOffset, setPanOffset] = useState({ x: 0, y: 0 });
  const pinchRef = useRef<{ startDist: number; startZoom: number; startPan: { x: number; y: number }; startCenter: { x: number; y: number } } | null>(null);

  // Bind the video stream to the <video> element
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;
    if (desktopRemoteStream) {
      video.srcObject = desktopRemoteStream;
      video.play().catch(() => {});
      // Auto-focus container so keyboard events work immediately
      containerRef.current?.focus();
    } else {
      video.srcObject = null;
    }
  }, [desktopRemoteStream]);

  // Track video resolution from the stream
  useEffect(() => {
    const video = videoRef.current;
    if (!video) return;

    const onResize = () => {
      if (video.videoWidth && video.videoHeight) {
        setVideoResolution({ w: video.videoWidth, h: video.videoHeight });
      }
    };
    video.addEventListener("resize", onResize);
    video.addEventListener("loadedmetadata", onResize);
    return () => {
      video.removeEventListener("resize", onResize);
      video.removeEventListener("loadedmetadata", onResize);
    };
  }, []);

  // Auto-hide toolbar after 3s of no mouse movement
  const resetHideTimer = useCallback(() => {
    setShowToolbar(true);
    if (hideTimer.current) clearTimeout(hideTimer.current);
    hideTimer.current = setTimeout(() => setShowToolbar(false), 3000);
  }, []);

  useEffect(() => {
    return () => {
      if (hideTimer.current) clearTimeout(hideTimer.current);
    };
  }, []);

  // Fullscreen change listener
  useEffect(() => {
    const onChange = () => setIsFullscreen(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  const toggleFullscreen = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else {
      el.requestFullscreen();
    }
  }, []);

  const handleDisconnect = useCallback(() => {
    stopDesktopStream();
    useStore.getState().setActiveView("session");
  }, []);

  // ── Input event helpers ────────────────────────────────────────────────

  const lastMoveRef = useRef(0);

  /** Compute normalized (0-1) video coords from a client-space point,
   *  accounting for pinch-to-zoom transform. */
  const getVideoCoords = useCallback(
    (clientX: number, clientY: number): { x: number; y: number } | null => {
      const video = videoRef.current;
      const container = containerRef.current;
      if (!video || !container || !video.videoWidth || !video.videoHeight) return null;

      // Use the container rect as stable reference (not affected by zoom/pan).
      // The video fills the container (w-full h-full), so container size = video
      // layout size before CSS transform.
      const cRect = container.getBoundingClientRect();
      const elW = cRect.width;
      const elH = cRect.height;

      // Position relative to the container's top-left
      const relX = clientX - cRect.left;
      const relY = clientY - cRect.top;

      // Inverse the CSS transform: translate(panOffset) scale(zoom)
      // with transformOrigin at center.  Forward transform maps layout point
      // (lx,ly) to screen point: sx = (lx - cx) * zoom + cx + panX.
      // Inverse: lx = (sx - cx - panX) / zoom + cx
      const cx = elW / 2;
      const cy = elH / 2;
      const localX = (relX - cx - panOffset.x) / zoom + cx;
      const localY = (relY - cy - panOffset.y) / zoom + cy;

      if (scaleMode === "fill") {
        return {
          x: Math.max(0, Math.min(1, localX / elW)),
          y: Math.max(0, Math.min(1, localY / elH)),
        };
      }

      // object-contain: letterboxed/pillarboxed — compute rendered video rect
      const vidW = video.videoWidth;
      const vidH = video.videoHeight;
      const elAR = elW / elH;
      const vidAR = vidW / vidH;

      let renderW: number, renderH: number, renderX: number, renderY: number;
      if (vidAR > elAR) {
        renderW = elW;
        renderH = elW / vidAR;
        renderX = 0;
        renderY = (elH - renderH) / 2;
      } else {
        renderH = elH;
        renderW = elH * vidAR;
        renderY = 0;
        renderX = (elW - renderW) / 2;
      }

      return {
        x: Math.max(0, Math.min(1, (localX - renderX) / renderW)),
        y: Math.max(0, Math.min(1, (localY - renderY) / renderH)),
      };
    },
    [scaleMode, zoom, panOffset],
  );

  const handleMouseMove = useCallback(
    (e: React.MouseEvent<HTMLVideoElement>) => {
      const now = performance.now();
      if (now - lastMoveRef.current < 33) return;
      lastMoveRef.current = now;
      const coords = getVideoCoords(e.clientX, e.clientY);
      if (coords) sendDesktopInput({ type: "mousemove", ...coords });
    },
    [getVideoCoords],
  );

  const handleMouseDown = useCallback(
    (e: React.MouseEvent<HTMLVideoElement>) => {
      e.preventDefault();
      // Ensure the container keeps focus so keyboard events work
      containerRef.current?.focus();
      const coords = getVideoCoords(e.clientX, e.clientY);
      if (coords) sendDesktopInput({ type: "mousedown", button: e.button, ...coords });
    },
    [getVideoCoords],
  );

  const handleMouseUp = useCallback(
    (e: React.MouseEvent<HTMLVideoElement>) => {
      const coords = getVideoCoords(e.clientX, e.clientY);
      if (coords) sendDesktopInput({ type: "mouseup", button: e.button, ...coords });
    },
    [getVideoCoords],
  );

  // Wheel — non-passive for preventDefault; bind on container to catch all scrolls
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const handler = (e: WheelEvent) => {
      e.preventDefault();
      const coords = getVideoCoords(e.clientX, e.clientY);
      if (coords) sendDesktopInput({ type: "wheel", dx: e.deltaX, dy: e.deltaY, ...coords });
    };
    container.addEventListener("wheel", handler, { passive: false });
    return () => container.removeEventListener("wheel", handler);
  }, [getVideoCoords]);

  const handleContextMenu = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
  }, []);

  // ── Touch: single-finger → mouse events, two-finger → pinch/pan ──────

  const activeTouchCountRef = useRef(0);

  const handleTouchStart = useCallback(
    (e: React.TouchEvent<HTMLVideoElement>) => {
      activeTouchCountRef.current = e.touches.length;
      if (e.touches.length === 2) {
        // Start pinch
        const [t1, t2] = [e.touches[0], e.touches[1]];
        const dist = Math.hypot(t2.clientX - t1.clientX, t2.clientY - t1.clientY);
        pinchRef.current = {
          startDist: dist,
          startZoom: zoom,
          startPan: { ...panOffset },
          startCenter: { x: (t1.clientX + t2.clientX) / 2, y: (t1.clientY + t2.clientY) / 2 },
        };
        return;
      }
      if (e.touches.length === 1) {
        const t = e.changedTouches[0];
        if (!t) return;
        const coords = getVideoCoords(t.clientX, t.clientY);
        if (coords) sendDesktopInput({ type: "mousedown", button: 0, ...coords });
      }
    },
    [getVideoCoords, zoom, panOffset],
  );

  const handleTouchMove = useCallback(
    (e: React.TouchEvent<HTMLVideoElement>) => {
      if (e.touches.length === 2 && pinchRef.current) {
        const [t1, t2] = [e.touches[0], e.touches[1]];
        const dist = Math.hypot(t2.clientX - t1.clientX, t2.clientY - t1.clientY);
        const newZoom = Math.max(1, Math.min(5, pinchRef.current.startZoom * (dist / pinchRef.current.startDist)));
        setZoom(newZoom);

        // Pan: track center movement
        const cx = (t1.clientX + t2.clientX) / 2;
        const cy = (t1.clientY + t2.clientY) / 2;
        setPanOffset({
          x: pinchRef.current.startPan.x + (cx - pinchRef.current.startCenter.x),
          y: pinchRef.current.startPan.y + (cy - pinchRef.current.startCenter.y),
        });
        return;
      }
      if (e.touches.length === 1 && activeTouchCountRef.current === 1) {
        const now = performance.now();
        if (now - lastMoveRef.current < 33) return;
        lastMoveRef.current = now;
        const t = e.changedTouches[0];
        if (!t) return;
        const coords = getVideoCoords(t.clientX, t.clientY);
        if (coords) sendDesktopInput({ type: "mousemove", ...coords });
      }
    },
    [getVideoCoords, zoom],
  );

  const handleTouchEnd = useCallback(
    (e: React.TouchEvent<HTMLVideoElement>) => {
      if (e.touches.length < 2) {
        if (pinchRef.current) {
          pinchRef.current = null;
          // Reset pan/zoom to 1 if zoom is close to 1
          if (zoom <= 1.05) {
            setZoom(1);
            setPanOffset({ x: 0, y: 0 });
          }
        }
      }
      if (e.touches.length === 0 && activeTouchCountRef.current === 1) {
        const t = e.changedTouches[0];
        if (!t) return;
        const coords = getVideoCoords(t.clientX, t.clientY);
        if (coords) sendDesktopInput({ type: "mouseup", button: 0, ...coords });
      }
      activeTouchCountRef.current = e.touches.length;
    },
    [getVideoCoords, zoom],
  );

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.metaKey || (e.ctrlKey && e.key === "Tab")) return;
    e.preventDefault();
    sendDesktopInput({ type: "keydown", key: e.key });
  }, []);

  const handleKeyUp = useCallback((e: React.KeyboardEvent) => {
    e.preventDefault();
    sendDesktopInput({ type: "keyup", key: e.key });
  }, []);

  // ── Virtual keyboard helpers ───────────────────────────────────────────

  const toggleVirtualKeyboard = useCallback(() => {
    if (vkbActive) {
      vkbInputRef.current?.blur();
    } else {
      setVkbActive(true);
      // Delay focus to next frame so state update renders the input first
      requestAnimationFrame(() => vkbInputRef.current?.focus());
    }
  }, [vkbActive]);

  const handleVkbKeyDown = useCallback((e: React.KeyboardEvent<HTMLInputElement>) => {
    // Forward special keys — let printable chars go through the input event
    if (e.key.length > 1 || e.ctrlKey || e.altKey || e.metaKey) {
      e.preventDefault();
      sendDesktopInput({ type: "keydown", key: e.key });
    }
  }, []);

  const handleVkbKeyUp = useCallback((e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key.length > 1 || e.ctrlKey || e.altKey || e.metaKey) {
      sendDesktopInput({ type: "keyup", key: e.key });
    }
  }, []);

  const handleVkbInput = useCallback((e: React.FormEvent<HTMLInputElement>) => {
    const input = e.currentTarget;
    const data = (e.nativeEvent as InputEvent).data;
    if (data) {
      // Send each character as a text event (handles IME, autocorrect, paste)
      for (const char of data) {
        sendDesktopInput({ type: "text", text: char });
      }
    }
    // Clear the input so it stays ready for the next character
    input.value = "";
  }, []);

  const handleVkbBlur = useCallback(() => {
    setVkbActive(false);
    if (vkbInputRef.current) vkbInputRef.current.value = "";
  }, []);

  // ── Offline / no stream states ─────────────────────────────────────────

  if (desktopStatus === "offline") {
    return (
      <div className="flex items-center justify-center h-full text-cc-muted text-sm">
        <div className="text-center space-y-3">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1" className="w-12 h-12 mx-auto opacity-30">
            <rect x="2" y="3" width="12" height="8" rx="1" />
            <path d="M5 14h6M8 11v3" />
          </svg>
          <p className="text-cc-fg font-medium">Node offline</p>
          <p className="text-xs opacity-60">Connection lost after {DESKTOP_MAX_RECONNECT} retries</p>
          <button
            onClick={() => retryDesktopStream()}
            className="px-4 py-1.5 rounded-lg bg-cc-primary text-white text-xs font-medium hover:bg-cc-primary-hover transition-colors cursor-pointer"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (!desktopStreamActive && desktopStatus !== "connecting" && desktopStatus !== "reconnecting") {
    return (
      <div className="flex items-center justify-center h-full text-cc-muted text-sm">
        <div className="text-center space-y-2">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1" className="w-12 h-12 mx-auto opacity-30">
            <rect x="2" y="3" width="12" height="8" rx="1" />
            <path d="M5 14h6M8 11v3" />
          </svg>
          <p>No desktop stream active</p>
          <p className="text-xs opacity-60">Connect to a remote node with screen sharing enabled</p>
        </div>
      </div>
    );
  }

  // Video transform style for pinch-to-zoom
  const videoStyle: React.CSSProperties = zoom > 1
    ? { transform: `translate(${panOffset.x}px, ${panOffset.y}px) scale(${zoom})`, transformOrigin: "center center" }
    : {};

  return (
    <div
      ref={containerRef}
      tabIndex={0}
      className="relative w-full h-full bg-black overflow-hidden select-none outline-none"
      onMouseMove={resetHideTimer}
      onMouseEnter={resetHideTimer}
      onKeyDown={handleKeyDown}
      onKeyUp={handleKeyUp}
    >
      {/* Video element */}
      <video
        ref={videoRef}
        autoPlay
        playsInline
        muted
        style={videoStyle}
        className={`w-full h-full ${scaleMode === "fit" ? "object-contain" : "object-cover"} cursor-default`}
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

      {/* Reconnecting overlay */}
      {(desktopStatus === "reconnecting" || desktopStatus === "connecting") && (
        <div className="absolute inset-0 flex items-center justify-center bg-black/60">
          <div className="text-center space-y-2">
            <div className="w-6 h-6 border-2 border-white/30 border-t-white rounded-full animate-spin mx-auto" />
            <p className="text-white/70 text-sm">
              {desktopStatus === "reconnecting" ? "Reconnecting..." : "Connecting..."}
            </p>
          </div>
        </div>
      )}

      {/* Floating toolbar */}
      <div
        className={`absolute bottom-4 left-1/2 -translate-x-1/2 flex items-center gap-1 px-2 py-1.5 rounded-xl bg-black/70 backdrop-blur-sm border border-white/10 transition-opacity duration-300 ${
          showToolbar ? "opacity-100" : "opacity-0 pointer-events-none"
        }`}
      >
        {/* Scale mode toggle */}
        <ToolbarButton
          title={scaleMode === "fit" ? "Fill screen" : "Fit to window"}
          onClick={() => setScaleMode((m) => (m === "fit" ? "fill" : "fit"))}
        >
          {scaleMode === "fit" ? (
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-4 h-4">
              <path d="M2 6V2h4M14 6V2h-4M2 10v4h4M14 10v4h-4" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          ) : (
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-4 h-4">
              <path d="M5 2H2v3M11 2h3v3M5 14H2v-3M11 14h3v-3" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          )}
        </ToolbarButton>

        {/* Fullscreen */}
        <ToolbarButton
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
        </ToolbarButton>

        {/* Virtual keyboard toggle */}
        <ToolbarButton
          title={vkbActive ? "Hide keyboard" : "Show keyboard"}
          onClick={toggleVirtualKeyboard}
          active={vkbActive}
        >
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" className="w-4 h-4">
            <rect x="1" y="4" width="14" height="9" rx="1.5" />
            <path d="M4 7h1M7 7h2M11 7h1M4 9.5h1M7 9.5h2M11 9.5h1M5 11.5h6" strokeLinecap="round" />
          </svg>
        </ToolbarButton>

        {/* Clipboard: copy from remote */}
        <ToolbarButton title="Copy from remote" onClick={handleCopyFromRemote}>
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" className="w-4 h-4">
            <rect x="5" y="1.5" width="8" height="10" rx="1.2" />
            <path d="M3 4.5H2.5a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h7a1 1 0 0 0 1-1V14" />
          </svg>
        </ToolbarButton>

        {/* Clipboard: paste to remote */}
        <ToolbarButton title="Paste to remote" onClick={handlePasteToRemote}>
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" className="w-4 h-4">
            <rect x="2.5" y="2.5" width="11" height="12" rx="1.2" />
            <path d="M5.5 1.5h5a1 1 0 0 1 1 1v1h-7v-1a1 1 0 0 1 1-1z" />
            <path d="M5.5 8h5M5.5 10.5h3" strokeLinecap="round" />
          </svg>
        </ToolbarButton>

        {/* Reset zoom (only when zoomed) */}
        {zoom > 1.05 && (
          <ToolbarButton
            title="Reset zoom"
            onClick={() => { setZoom(1); setPanOffset({ x: 0, y: 0 }); }}
          >
            <span className="text-[10px] font-mono font-bold">{zoom.toFixed(1)}x</span>
          </ToolbarButton>
        )}

        {/* Divider */}
        <div className="w-px h-5 bg-white/20 mx-1" />

        {/* Quality stats + resolution */}
        <div className="flex items-center gap-1.5 px-1">
          {videoResolution && (
            <span className="text-[10px] text-white/50 font-mono tabular-nums">
              {videoResolution.w}x{videoResolution.h}
            </span>
          )}
          {desktopStats && (
            <>
              <span className={`text-[10px] font-mono tabular-nums ${fpsColor(desktopStats.fps)}`}>
                {desktopStats.fps}fps
              </span>
              {desktopStats.bitrate > 0 && (
                <span className="text-[10px] text-white/50 font-mono tabular-nums">
                  {desktopStats.bitrate}k
                </span>
              )}
              {desktopStats.rtt > 0 && (
                <span className={`text-[10px] font-mono tabular-nums ${rttColor(desktopStats.rtt)}`}>
                  {desktopStats.rtt}ms
                </span>
              )}
            </>
          )}
        </div>

        {/* Divider */}
        <div className="w-px h-5 bg-white/20 mx-1" />

        {/* Disconnect */}
        <ToolbarButton title="Disconnect" onClick={handleDisconnect} danger>
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-4 h-4">
            <path d="M12 4L4 12M4 4l8 8" strokeLinecap="round" />
          </svg>
        </ToolbarButton>
      </div>

      {/* Clipboard toast */}
      {clipToast && (
        <div className="absolute top-4 left-1/2 -translate-x-1/2 px-3 py-1.5 rounded-lg bg-black/80 text-white text-xs font-medium backdrop-blur-sm border border-white/10 animate-fade-in">
          {clipToast}
        </div>
      )}
    </div>
  );
}

// ── Quality color helpers ─────────────────────────────────────────────────

const DESKTOP_MAX_RECONNECT = 3;

function fpsColor(fps: number): string {
  if (fps < 15) return "text-red-400";
  if (fps < 25) return "text-yellow-400";
  return "text-green-400";
}

function rttColor(rtt: number): string {
  if (rtt > 300) return "text-red-400";
  if (rtt > 150) return "text-yellow-400";
  return "text-green-400";
}

// ── Toolbar button ────────────────────────────────────────────────────────

function ToolbarButton({
  children,
  onClick,
  title,
  danger,
  active,
}: {
  children: React.ReactNode;
  onClick: () => void;
  title: string;
  danger?: boolean;
  active?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      onMouseDown={(e) => e.preventDefault()} // prevent focus steal from container
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
