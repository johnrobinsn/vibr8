import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useStore } from "./store.js";
import { api } from "./api.js";
import { Sidebar } from "./components/Sidebar.js";
import { ChatView } from "./components/ChatView.js";
import { TopBar } from "./components/TopBar.js";
import { HomePage } from "./components/HomePage.js";
import { TaskPanel } from "./components/TaskPanel.js";
import { DesktopView } from "./components/DesktopView.js";
import { EditorPanel } from "./components/EditorPanel.js";
import { Playground } from "./components/Playground.js";
import { SecondScreen } from "./components/SecondScreen.js";
import { TerminalView } from "./components/TerminalView.js";
import { LoginPage } from "./components/LoginPage.js";
import { CommandPalette } from "./components/CommandPalette.js";
import { SettingsPage } from "./components/SettingsPage.js";
import { connectSession, disconnectSession } from "./ws.js";
import { startWebRTC, setAudioInOnly } from "./webrtc.js";

function useHash() {
  return useSyncExternalStore(
    (cb) => { window.addEventListener("hashchange", cb); return () => window.removeEventListener("hashchange", cb); },
    () => window.location.hash,
  );
}

export default function App() {
  const [authState, setAuthState] = useState<"loading" | "login" | "authenticated">("loading");
  const darkMode = useStore((s) => s.darkMode);
  const currentSessionId = useStore((s) => s.currentSessionId);
  const sidebarOpen = useStore((s) => s.sidebarOpen);
  const taskPanelOpen = useStore((s) => s.taskPanelOpen);
  const homeResetKey = useStore((s) => s.homeResetKey);
  const activeTab = useStore((s) => s.activeTab);
  const activeView = useStore((s) => s.activeView);
  const isTerminalSession = useStore((s) => {
    if (!s.currentSessionId) return false;
    const sdk = s.sdkSessions.find((x) => x.sessionId === s.currentSessionId);
    return sdk?.backendType === "terminal";
  });
  const terminalIdsRef = useRef<string[]>([]);
  const terminalSessionIds = useStore((s) => {
    const ids = s.sdkSessions.filter((x) => x.backendType === "terminal").map((x) => x.sessionId);
    const prev = terminalIdsRef.current;
    if (ids.length === prev.length && ids.every((id, i) => id === prev[i])) return prev;
    terminalIdsRef.current = ids;
    return ids;
  });
  const commandPaletteOpen = useStore((s) => s.commandPaletteOpen);
  const [ring0SessionId, setRing0SessionId] = useState<string | null>(null);
  useEffect(() => {
    api.getRing0Status().then((s) => setRing0SessionId(s.sessionId ?? null)).catch(() => {});
  }, []);
  const previousSessionRef = useRef<string | null>(null);
  const hash = useHash();

  // Auth check on mount
  useEffect(() => {
    fetch("/api/auth/me")
      .then((r) => r.json())
      .then((data) => {
        if (!data.authEnabled || data.authenticated) {
          setAuthState("authenticated");
        } else {
          setAuthState("login");
        }
      })
      .catch(() => setAuthState("authenticated")); // If endpoint fails, assume no auth
  }, []);

  useEffect(() => {
    // Second screen manages its own dark mode independently — read hash
    // directly so this is resilient to HMR re-renders where reactive state
    // may briefly be stale.
    if (window.location.hash === "#/second-screen") return;
    document.documentElement.classList.toggle("dark", darkMode);
  }, [darkMode]);

  // Auto-reconnect audio if it was active before reload
  const audioReconnectedRef = useRef(false);
  useEffect(() => {
    if (audioReconnectedRef.current) return;
    const savedMode = localStorage.getItem("cc-audio-mode");
    if (!savedMode || savedMode === "off") return;

    // Wait for any session's CLI to be connected before starting WebRTC
    const unsub = useStore.subscribe((s) => {
      if (audioReconnectedRef.current) return;
      // Check if any session has CLI connected
      for (const [, connected] of s.cliConnected) {
        if (connected) {
          audioReconnectedRef.current = true;
          unsub();
          startWebRTC()
            .then(() => {
              // Restore saved audio mode (startWebRTC defaults to in_out)
              if (savedMode === "in_only") {
                setAudioInOnly();
              }
            })
            .catch((err) => {
              console.warn("[webrtc] Auto-reconnect failed:", err);
            });
          return;
        }
      }
    });
    return () => unsub();
  }, []);

  // Keyboard shortcuts: Escape to close overlays, Ctrl/Cmd+Alt+S to toggle sidebar
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        if (useStore.getState().commandPaletteOpen) { useStore.getState().setCommandPaletteOpen(false); return; }
        if (taskPanelOpen) { useStore.getState().setTaskPanelOpen(false); return; }
        if (sidebarOpen) { useStore.getState().setSidebarOpen(false); return; }
      }
      // Ctrl/Cmd+` — toggle between Ring0 and previous session
      if (e.key === "`" && (e.metaKey || e.ctrlKey) && ring0SessionId) {
        e.preventDefault();
        const s = useStore.getState();
        if (s.currentSessionId === ring0SessionId) {
          // Switch back to previous session
          const prevId = previousSessionRef.current;
          if (prevId) {
            if (s.currentSessionId) {
              const oldSdk = s.sdkSessions.find((x) => x.sessionId === s.currentSessionId);
              if (oldSdk?.backendType !== "terminal") disconnectSession(s.currentSessionId);
            }
            s.setCurrentSession(prevId);
            const newSdk = s.sdkSessions.find((x) => x.sessionId === prevId);
            if (newSdk?.backendType !== "terminal") connectSession(prevId);
          }
        } else {
          // Switch to Ring0, remember current
          previousSessionRef.current = s.currentSessionId;
          if (s.currentSessionId) {
            const oldSdk = s.sdkSessions.find((x) => x.sessionId === s.currentSessionId);
            if (oldSdk?.backendType !== "terminal") disconnectSession(s.currentSessionId);
          }
          s.setCurrentSession(ring0SessionId);
          connectSession(ring0SessionId);
        }
        return;
      }
      if (e.key === "k" && (e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey) {
        e.preventDefault();
        useStore.getState().toggleCommandPalette();
        return;
      }
      if (e.altKey && (e.metaKey || e.ctrlKey)) {
        if (e.key === "p") {
          e.preventDefault();
          useStore.getState().toggleCommandPalette();
          return;
        }
        if (e.key === "s") {
          e.preventDefault();
          const s = useStore.getState();
          s.setSidebarOpen(!s.sidebarOpen);
          return;
        }
      }
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [sidebarOpen, taskPanelOpen, ring0SessionId]);

  const sessionsLoaded = useStore((s) => s.sessionsLoaded);

  // Second screen bypasses auth — it uses pairing codes instead
  if (hash === "#/second-screen") {
    return <SecondScreen />;
  }

  if (authState === "loading") return null;
  if (authState === "login") return <LoginPage onLogin={() => setAuthState("authenticated")} />;

  if (hash === "#/playground") {
    return <Playground />;
  }

  if (hash === "#/settings" || hash.startsWith("#/settings/")) {
    return <SettingsPage />;
  }

  return (
    <div className="h-[100dvh] flex font-sans-ui bg-cc-bg text-cc-fg antialiased">
      {/* Mobile overlay backdrop */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 bg-black/30 z-30 md:hidden"
          onClick={() => useStore.getState().setSidebarOpen(false)}
        />
      )}

      {/* Sidebar — overlay on mobile, inline on desktop */}
      <div
        className={`
          fixed md:relative z-40 md:z-auto
          h-full shrink-0 transition-all duration-200
          ${sidebarOpen ? "w-[260px] translate-x-0" : "w-0 -translate-x-full md:w-0 md:-translate-x-full"}
          overflow-hidden
        `}
      >
        <Sidebar />
      </div>

      {/* Main area */}
      <div className="flex-1 flex flex-col min-w-0 overflow-hidden">
        <TopBar />
        <div className="flex-1 overflow-hidden relative">
          {/* Terminal sessions — kept mounted but hidden to preserve buffer */}
          {terminalSessionIds.map((id) => (
            <div key={id} className={`absolute inset-0 ${currentSessionId === id ? "" : "hidden"}`}>
              <TerminalView sessionId={id} visible={currentSessionId === id} />
            </div>
          ))}

          {/* Non-terminal content */}
          {!isTerminalSession && activeView !== "desktop" && (
            <>
              {/* Chat tab — visible when activeTab is "chat" or no session */}
              <div className={`absolute inset-0 ${activeTab === "chat" || !currentSessionId ? "" : "hidden"}`}>
                {currentSessionId ? (
                  <ChatView sessionId={currentSessionId} />
                ) : (
                  <HomePage key={homeResetKey} />
                )}
              </div>

              {/* Editor tab */}
              {currentSessionId && activeTab === "editor" && (
                <div className="absolute inset-0">
                  <EditorPanel sessionId={currentSessionId} />
                </div>
              )}
            </>
          )}

          {/* Desktop view (node-level, not session-specific — renders over any session) */}
          {activeView === "desktop" && (
            <div className="absolute inset-0">
              <DesktopView sessionId={currentSessionId || ""} />
            </div>
          )}
        </div>
      </div>

      {/* Task panel — overlay on mobile, inline on desktop */}
      {currentSessionId && (
        <>
          {/* Mobile overlay backdrop */}
          {taskPanelOpen && (
            <div
              className="fixed inset-0 bg-black/30 z-30 lg:hidden"
              onClick={() => useStore.getState().setTaskPanelOpen(false)}
            />
          )}

          <div
            className={`
              fixed lg:relative z-40 lg:z-auto right-0 top-0
              h-full shrink-0 transition-all duration-200
              ${taskPanelOpen ? "w-[280px] translate-x-0" : "w-0 translate-x-full lg:w-0 lg:translate-x-full"}
              overflow-hidden
            `}
          >
            <TaskPanel sessionId={currentSessionId} />
          </div>
        </>
      )}

      {commandPaletteOpen && <CommandPalette />}
    </div>
  );
}
