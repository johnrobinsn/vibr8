import { useState, useEffect, useCallback, useRef } from "react";
import { useStore } from "../store.js";
import { api } from "../api.js";
import { MessageFeed } from "./MessageFeed.js";
import { handleMessage } from "../ws.js";

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
  const [pairedClientId, setPairedClientId] = useState<string | null>(null);
  const pushedContent = useStore((s) => s.secondScreenContent);

  // Set client role to secondscreen on mount
  useEffect(() => {
    useStore.getState().setClientRole("secondscreen");
    return () => {
      useStore.getState().setClientRole("primary");
    };
  }, []);

  // Force dark mode
  useEffect(() => {
    document.documentElement.classList.add("dark");
    return () => {
      // Restore original dark mode on unmount
      const isDark = useStore.getState().darkMode;
      document.documentElement.classList.toggle("dark", isDark);
    };
  }, []);

  // Check pairing status on mount
  useEffect(() => {
    api.secondScreenStatus(clientId).then((status) => {
      if (status.paired && status.role === "secondscreen" && status.pairedClientId) {
        setPairingState("paired");
        setPairedClientId(status.pairedClientId);
      } else {
        setPairingState("unpaired");
      }
    }).catch(() => setPairingState("unpaired"));
  }, [clientId]);

  // When paired, open a dedicated WebSocket to Ring0 (independent of primary UI)
  const wsRef = useRef<WebSocket | null>(null);
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

      ws.onmessage = (event) => handleMessage(RING0_SESSION_ID, event, ws);

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
  }, [pairingState]);

  // Generate pairing code
  const generateCode = useCallback(async () => {
    try {
      const { code } = await api.secondScreenPairCode(clientId);
      setPairingCode(code);
    } catch (err) {
      console.error("[second-screen] Failed to generate code:", err);
    }
  }, [clientId]);

  // Auto-generate code when unpaired
  useEffect(() => {
    if (pairingState === "unpaired" && !pairingCode) {
      generateCode();
    }
  }, [pairingState, pairingCode, generateCode]);

  // Listen for pairing completion via WebSocket
  useEffect(() => {
    if (pairingState !== "unpaired") return;

    function handleMessage(event: MessageEvent) {
      try {
        const data = JSON.parse(event.data);
        if (data.type === "second_screen_paired") {
          setPairingState("paired");
          setPairedClientId(data.pairedClientId);
          setPairingCode(null);
        }
      } catch {
        // ignore
      }
    }

    // We need to listen on any active WebSocket connection
    // The pairing notification comes through the browser WS
    // For now, poll status as a fallback
    const interval = setInterval(async () => {
      try {
        const status = await api.secondScreenStatus(clientId);
        if (status.paired && status.role === "secondscreen") {
          setPairingState("paired");
          setPairedClientId(status.pairedClientId ?? null);
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
    setPairedClientId(null);
    setPairingCode(null);
    if (wsRef.current) { wsRef.current.close(); wsRef.current = null; }
    useStore.getState().setCurrentSession(null);
  }, [clientId]);

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

  // Paired — show Ring0 chat feed
  return (
    <div className="h-[100dvh] flex flex-col bg-cc-bg text-cc-fg">
      {/* Minimal status bar */}
      <div className="shrink-0 flex items-center justify-between px-4 py-2 border-b border-cc-border text-xs text-cc-fg-muted">
        <div className="flex items-center gap-2">
          <span className="inline-block w-2 h-2 rounded-full bg-green-500" />
          <span>Second Screen — Ring0</span>
        </div>
        <button
          onClick={handleUnpair}
          className="text-cc-fg-muted hover:text-cc-fg transition-colors"
        >
          Unpair
        </button>
      </div>

      {/* Content area — always show Ring0 feed when paired */}
      <div className="flex-1 overflow-hidden">
        {pushedContent ? (
          <PushedContentView content={pushedContent} />
        ) : (
          <MessageFeed sessionId={RING0_SESSION_ID} />
        )}
      </div>
    </div>
  );
}

function PushedContentView({ content }: { content: { type: string; content: string } }) {
  const handleClear = () => useStore.getState().setSecondScreenContent(null);
  const [imgError, setImgError] = useState(false);
  const [imgLoaded, setImgLoaded] = useState(false);

  if (content.type === "image") {
    const url = content.content.trim();
    console.log("[second-screen] Rendering image:", url);
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
            onError={(e) => {
              console.error("[second-screen] Image load error:", url, e);
              setImgError(true);
            }}
          />
        )}
        <button onClick={handleClear} className="mt-4 text-xs text-cc-fg-muted hover:text-cc-fg">
          Back to Ring0
        </button>
      </div>
    );
  }

  // Default: markdown/text
  return (
    <div className="h-full overflow-auto p-8">
      <div className="max-w-3xl mx-auto">
        <pre className="whitespace-pre-wrap text-sm leading-relaxed">{content.content}</pre>
      </div>
      <div className="text-center mt-4">
        <button onClick={handleClear} className="text-xs text-cc-fg-muted hover:text-cc-fg">
          Back to Ring0
        </button>
      </div>
    </div>
  );
}
