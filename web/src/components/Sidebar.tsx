import { useState, useEffect, useCallback, useRef, Fragment } from "react";
import { useStore } from "../store.js";
import { api } from "../api.js";
import { connectSession, disconnectSession } from "../ws.js";
import { startWebRTC, stopWebRTC, stopDesktopStream } from "../webrtc.js";
import { destroyTerminal } from "./TerminalView.js";
import { EnvManager } from "./EnvManager.js";

export function Sidebar() {
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
  const [editingName, setEditingName] = useState("");
  const [showEnvManager, setShowEnvManager] = useState(false);
  const [authEnabled, setAuthEnabled] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [ring0SessionId, setRing0SessionId] = useState<string | null>(null);
  const [ring0EventsMuted, setRing0EventsMuted] = useState(false);
  const [confirmArchiveId, setConfirmArchiveId] = useState<string | null>(null);
  const editInputRef = useRef<HTMLInputElement>(null);
  const sessions = useStore((s) => s.sessions);
  const sdkSessions = useStore((s) => s.sdkSessions);
  const currentSessionId = useStore((s) => s.currentSessionId);
  const setCurrentSession = useStore((s) => s.setCurrentSession);
  const darkMode = useStore((s) => s.darkMode);
  const toggleDarkMode = useStore((s) => s.toggleDarkMode);
  const notificationSound = useStore((s) => s.notificationSound);
  const toggleNotificationSound = useStore((s) => s.toggleNotificationSound);
  const cliConnected = useStore((s) => s.cliConnected);
  const sessionStatus = useStore((s) => s.sessionStatus);
  const removeSession = useStore((s) => s.removeSession);
  const sessionNames = useStore((s) => s.sessionNames);
  const recentlyRenamed = useStore((s) => s.recentlyRenamed);
  const pendingPermissions = useStore((s) => s.pendingPermissions);
  const sidebarOpen = useStore((s) => s.sidebarOpen);
  const nodes = useStore((s) => s.nodes);
  const androidDevices = useStore((s) => s.androidDevices);
  const activeNodeId = useStore((s) => s.activeNodeId);
  const desktopStreamActive = useStore((s) => s.desktopStreamActive);
  const activeView = useStore((s) => s.activeView);
  const splitViewActive = useStore((s) => s.splitViewActive);
  const sidebarListRef = useRef<HTMLDivElement>(null);
  const newSessionRef = useRef<HTMLButtonElement>(null);
  const longPressTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const longPressTriggered = useRef(false);
  const touchStartY = useRef<number>(0);
  const touchMoved = useRef(false);
  const touchHandled = useRef(false);

  // Focus the current session (or first session, or New Session button) when sidebar opens
  // Skip on initial mount — only focus when user actively toggles the sidebar open
  const sidebarMountedRef = useRef(false);
  useEffect(() => {
    if (!sidebarMountedRef.current) {
      sidebarMountedRef.current = true;
      return;
    }
    if (!sidebarOpen) return;
    requestAnimationFrame(() => {
      // Try current session first
      if (currentSessionId && sidebarListRef.current) {
        const btn = sidebarListRef.current.querySelector<HTMLElement>(`[data-session-id="${currentSessionId}"]`);
        if (btn) { btn.focus(); return; }
      }
      // Fall back to first session button
      if (sidebarListRef.current) {
        const first = sidebarListRef.current.querySelector<HTMLElement>("[data-session-btn]");
        if (first) { first.focus(); return; }
      }
      // Fall back to New Session button
      newSessionRef.current?.focus();
    });
  }, [sidebarOpen]); // eslint-disable-line react-hooks/exhaustive-deps

  // Check if auth is enabled
  useEffect(() => {
    fetch("/api/auth/me").then((r) => r.json()).then((data) => {
      if (data.authEnabled) setAuthEnabled(true);
    }).catch(() => {});
  }, []);

  // Poll for SDK sessions on mount and when active node changes
  useEffect(() => {
    let active = true;
    let isFirstPoll = true;
    async function poll() {
      try {
        const [list, ring0Status, nodeList, androidList] = await Promise.all([
          api.listSessions(activeNodeId),
          api.getRing0Status().catch(() => ({ enabled: false, eventsMuted: false, sessionId: null })),
          api.listNodes().catch(() => []),
          api.listAndroidDevices().catch(() => []),
        ]);
        if (active) {
          setRing0SessionId(ring0Status.sessionId ?? null);
          setRing0EventsMuted(ring0Status.eventsMuted ?? false);
          useStore.getState().setSdkSessions(list);
          if (nodeList.length > 0) {
            useStore.getState().setNodes(nodeList);
          }
          useStore.getState().setAndroidDevices(androidList);
          // Hydrate session names from server (server is source of truth for auto-generated names)
          const store = useStore.getState();
          for (const s of list) {
            if (s.name && (!store.sessionNames.has(s.sessionId) || /^[A-Z][a-z]+ [A-Z][a-z]+$/.test(store.sessionNames.get(s.sessionId)!))) {
              const currentStoreName = store.sessionNames.get(s.sessionId);
              const hadRandomName = !!currentStoreName && /^[A-Z][a-z]+ [A-Z][a-z]+$/.test(currentStoreName);
              if (currentStoreName !== s.name) {
                store.setSessionName(s.sessionId, s.name);
                if (hadRandomName) {
                  store.markRecentlyRenamed(s.sessionId);
                }
              }
            }
          }

          // On first load, validate/restore the current session
          if (isFirstPoll) {
            const activeSessions = list.filter((s) => !s.archived);
            // Try per-node saved session first, then fall back to global
            const savedForNode = localStorage.getItem(`cc-node-session-${activeNodeId}`);
            const currentId = (savedForNode && activeSessions.some((s) => s.sessionId === savedForNode))
              ? savedForNode
              : store.currentSessionId;
            // Check if the eagerly-restored session still exists and is active
            const currentValid = currentId && activeSessions.some((s) => s.sessionId === currentId);

            if (currentValid) {
              // Session exists — restore and connect it
              const target = activeSessions.find((s) => s.sessionId === currentId)!;
              const isTerminal = target.backendType === "terminal";
              if (currentId !== store.currentSessionId) {
                store.setCurrentSession(currentId);
              }
              store.setPendingFocus(isTerminal ? "terminal" : "composer");
              store.setSidebarOpen(false);
              if (!isTerminal) {
                connectSession(currentId);
              }
            } else if (activeSessions.length > 0) {
              // Saved session gone — pick most recent
              const target = activeSessions.sort((a, b) => (b.createdAt ?? 0) - (a.createdAt ?? 0))[0];
              const isTerminal = target.backendType === "terminal";
              store.setCurrentSession(target.sessionId);
              store.setPendingFocus(isTerminal ? "terminal" : "composer");
              store.setSidebarOpen(false);
              if (!isTerminal) {
                connectSession(target.sessionId);
              }
            } else {
              // No active sessions — clear any stale session ID, show sidebar
              if (currentId) store.setCurrentSession(null);
              store.setSidebarOpen(true);
            }
          }
          isFirstPoll = false;
        }
      } catch {
        // server not ready
      }
    }
    poll();
    const interval = setInterval(poll, 5000);
    return () => {
      active = false;
      clearInterval(interval);
    };
  }, [activeNodeId]);

  function handleSelectSession(sessionId: string) {
    const s = useStore.getState();
    const newSdk = s.sdkSessions.find((x) => x.sessionId === sessionId);
    const isTerminal = newSdk?.backendType === "terminal";
    if (currentSessionId !== sessionId) {
      // Disconnect from old session (skip terminal sessions — they don't use WsBridge)
      if (currentSessionId) {
        const oldSdk = s.sdkSessions.find((x) => x.sessionId === currentSessionId);
        if (oldSdk?.backendType !== "terminal") {
          disconnectSession(currentSessionId);
        }
      }
      setCurrentSession(sessionId);
      // Terminal sessions use their own WebSocket — don't connect via WsBridge
      if (!isTerminal) {
        connectSession(sessionId);
      }
    }
    // Switch back to session view (from desktop or any other view)
    s.setActiveView("session");
    // Signal where focus should land
    s.setPendingFocus(isTerminal ? "terminal" : "composer");
    // Close sidebar on mobile
    if (window.innerWidth < 768) {
      useStore.getState().setSidebarOpen(false);
    }
  }

  function handleNewSession() {
    if (currentSessionId) {
      const oldSdk = useStore.getState().sdkSessions.find((x) => x.sessionId === currentSessionId);
      if (oldSdk?.backendType !== "terminal") {
        disconnectSession(currentSessionId);
      }
    }
    useStore.getState().newSession();
    useStore.getState().setActiveView("session");
    useStore.getState().setPendingFocus("home");
    if (window.innerWidth < 768) {
      useStore.getState().setSidebarOpen(false);
    }
  }

  async function handleNodeSwitch(nodeId: string) {
    if (nodeId === activeNodeId) return;
    // Disconnect current session
    const s = useStore.getState();
    if (s.currentSessionId) {
      // Save current session for this node before switching
      localStorage.setItem(`cc-node-session-${activeNodeId}`, s.currentSessionId);
      const oldSdk = s.sdkSessions.find((x) => x.sessionId === s.currentSessionId);
      if (oldSdk?.backendType !== "terminal") {
        disconnectSession(s.currentSessionId);
      }
    }
    s.setCurrentSession(null);
    s.clearSessionState();
    s.setActiveNode(nodeId);
    // Notify hub of active node change (for voice routing)
    api.activateNode(nodeId).catch((err) => {
      console.warn("[sidebar] Failed to activate node:", err);
    });
  }

  // Focus edit input when entering edit mode
  useEffect(() => {
    if (editingSessionId && editInputRef.current) {
      editInputRef.current.focus();
      editInputRef.current.select();
    }
  }, [editingSessionId]);

  function confirmRename() {
    if (editingSessionId && editingName.trim()) {
      const sid = editingSessionId;
      const newName = editingName.trim();
      const oldName = useStore.getState().sessionNames.get(sid);
      useStore.getState().setSessionName(sid, newName);
      api.renameSession(sid, newName).catch(() => {
        // Revert on failure (e.g. Ring0 cannot be renamed)
        if (oldName != null) useStore.getState().setSessionName(sid, oldName);
      });
    }
    setEditingSessionId(null);
    setEditingName("");
  }

  function cancelRename() {
    setEditingSessionId(null);
    setEditingName("");
  }

  const handleDeleteSession = useCallback(async (e: React.MouseEvent, sessionId: string) => {
    e.stopPropagation();
    try {
      stopWebRTC();
      disconnectSession(sessionId);
      await api.deleteSession(sessionId);
    } catch {
      // best-effort
    }
    removeSession(sessionId);
  }, [removeSession]);

  const handleArchiveSession = useCallback((e: React.MouseEvent, sessionId: string) => {
    e.stopPropagation();
    // Check if session uses a worktree — if so, ask for confirmation
    const sdkInfo = sdkSessions.find((s) => s.sessionId === sessionId);
    const bridgeState = sessions.get(sessionId);
    const isWorktree = bridgeState?.is_worktree || sdkInfo?.isWorktree || false;
    if (isWorktree) {
      setConfirmArchiveId(sessionId);
      return;
    }
    doArchive(sessionId);
  }, [sdkSessions, sessions]);

  const handleCloseTerminal = useCallback(async (e: React.MouseEvent, sessionId: string) => {
    e.stopPropagation();
    try {
      destroyTerminal(sessionId);
      await api.deleteSession(sessionId);
    } catch {
      // best-effort
    }
    removeSession(sessionId);
    if (useStore.getState().currentSessionId === sessionId) {
      useStore.getState().newSession();
    }
    // Refresh session list
    try {
      const list = await api.listSessions(activeNodeId);
      useStore.getState().setSdkSessions(list);
    } catch {
      // best-effort
    }
  }, [removeSession, activeNodeId]);

  const doArchive = useCallback(async (sessionId: string, force?: boolean) => {
    try {
      stopWebRTC();
      disconnectSession(sessionId);
      await api.archiveSession(sessionId, force ? { force: true } : undefined);
    } catch {
      // best-effort
    }
    if (useStore.getState().currentSessionId === sessionId) {
      useStore.getState().newSession();
    }
    try {
      const list = await api.listSessions(activeNodeId);
      useStore.getState().setSdkSessions(list);
    } catch {
      // best-effort
    }
  }, [activeNodeId]);

  const confirmArchive = useCallback(() => {
    if (confirmArchiveId) {
      doArchive(confirmArchiveId, true);
      setConfirmArchiveId(null);
    }
  }, [confirmArchiveId, doArchive]);

  const cancelArchive = useCallback(() => {
    setConfirmArchiveId(null);
  }, []);

  const handleUnarchiveSession = useCallback(async (e: React.MouseEvent, sessionId: string) => {
    e.stopPropagation();
    try {
      await api.unarchiveSession(sessionId);
    } catch {
      // best-effort
    }
    try {
      const list = await api.listSessions(activeNodeId);
      useStore.getState().setSdkSessions(list);
    } catch {
      // best-effort
    }
  }, [activeNodeId]);

  // Combine sessions: SDK list is source of truth (filtered by active node),
  // enriched with live bridge state where available
  const allSessionList = sdkSessions.map((s) => {
    const bridgeState = sessions.get(s.sessionId);
    return {
      id: s.sessionId,
      isRing0: s.isRing0 ?? false,
      model: bridgeState?.model || s.model || "",
      cwd: bridgeState?.cwd || s.cwd || "",
      gitBranch: bridgeState?.git_branch || "",
      isWorktree: bridgeState?.is_worktree || s.isWorktree || false,
      gitAhead: bridgeState?.git_ahead || 0,
      gitBehind: bridgeState?.git_behind || 0,
      linesAdded: bridgeState?.total_lines_added || 0,
      linesRemoved: bridgeState?.total_lines_removed || 0,
      isConnected: cliConnected.get(s.sessionId) ?? false,
      status: sessionStatus.get(s.sessionId) ?? null,
      sdkState: s.state ?? null,
      createdAt: s.createdAt ?? 0,
      lastPromptedAt: s.lastPromptedAt ?? 0,
      archived: s.archived ?? false,
      backendType: s.backendType || bridgeState?.backend_type || "claude",
      controlledBy: bridgeState?.controlledBy,
    };
  }).sort((a, b) => {
    // Ring0 always first
    if (a.isRing0) return -1;
    if (b.isRing0) return 1;
    // MRU order, fallback to createdAt
    const aTime = a.lastPromptedAt || a.createdAt;
    const bTime = b.lastPromptedAt || b.createdAt;
    return bTime - aTime;
  });

  const activeSessions = allSessionList.filter((s) => !s.archived);
  const archivedSessions = allSessionList.filter((s) => s.archived);
  const currentSession = currentSessionId ? allSessionList.find((s) => s.id === currentSessionId) : null;
  const logoSrc = currentSession?.backendType === "codex" ? "/logo-codex.svg" : "/logo.svg";

  function startRename(id: string, currentLabel: string) {
    const session = allSessionList.find((s) => s.id === id);
    if (session?.isRing0) return; // Ring0 cannot be renamed
    setEditingSessionId(id);
    setEditingName(currentLabel);
  }

  function renderSessionItem(s: typeof allSessionList[number], options?: { isArchived?: boolean }) {
    const isActive = currentSessionId === s.id;
    const name = sessionNames.get(s.id);
    const shortId = s.id.slice(0, 8);
    const label = name || s.model || shortId;
    const dirName = s.cwd ? s.cwd.split("/").pop() : "";
    const isRunning = s.status === "running";
    const isWaiting = s.status === "waiting_for_permission";
    const isCompacting = s.status === "compacting";
    const isEditing = editingSessionId === s.id;
    const permCount = pendingPermissions.get(s.id)?.size ?? 0;
    const archived = options?.isArchived;

    return (
      <div key={s.id} className={`relative group ${archived ? "opacity-60" : ""}`}>
        <button
          data-session-id={s.id}
          onClick={(e) => {
            // Skip synthesized click from touch — touchEnd already handled it
            if (touchHandled.current) { touchHandled.current = false; return; }
            // Skip when renaming (space in input triggers native button click)
            if (isEditing) return;
            // Skip navigation on double-click (let onDoubleClick handle it)
            if (e.detail === 2) return;
            handleSelectSession(s.id);
          }}
          onDoubleClick={(e) => {
            e.preventDefault();
            startRename(s.id, label);
          }}
          onTouchStart={(e) => {
            longPressTriggered.current = false;
            touchMoved.current = false;
            touchStartY.current = e.touches[0].clientY;
            longPressTimer.current = setTimeout(() => {
              longPressTriggered.current = true;
              startRename(s.id, label);
            }, 500);
          }}
          onTouchMove={(e) => {
            if (Math.abs(e.touches[0].clientY - touchStartY.current) > 10) {
              touchMoved.current = true;
              if (longPressTimer.current) { clearTimeout(longPressTimer.current); longPressTimer.current = null; }
            }
          }}
          onTouchEnd={() => {
            if (longPressTimer.current) { clearTimeout(longPressTimer.current); longPressTimer.current = null; }
            if (longPressTriggered.current || touchMoved.current) return;
            // Mark so the synthesized click from the browser is suppressed
            touchHandled.current = true;
            if (!isEditing) handleSelectSession(s.id);
          }}
          onTouchCancel={() => {
            if (longPressTimer.current) { clearTimeout(longPressTimer.current); longPressTimer.current = null; }
            longPressTriggered.current = false;
          }}
          onContextMenu={(e) => e.preventDefault()}
          onKeyDown={(e) => {
            // F2 or Ctrl+E to rename
            if (e.key === "F2" || (e.ctrlKey && e.key === "e")) {
              e.preventDefault();
              startRename(s.id, label);
              return;
            }
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
              e.preventDefault();
              const items = Array.from(
                e.currentTarget.closest("[data-session-list]")?.querySelectorAll<HTMLElement>("[data-session-btn]") ?? []
              );
              const idx = items.indexOf(e.currentTarget);
              const next = e.key === "ArrowDown" ? items[idx + 1] : items[idx - 1];
              next?.focus();
            }
          }}
          data-session-btn
          style={{ WebkitUserSelect: "none", userSelect: "none" } as React.CSSProperties}
          className={`w-full px-3 py-2.5 ${archived ? "pr-14" : "pr-8"} text-left rounded-[10px] transition-all duration-100 cursor-pointer ${
            isActive
              ? "bg-cc-active"
              : "hover:bg-cc-hover"
          }`}
        >
          <div className="flex items-center gap-2">
            <span className="relative flex shrink-0">
              <span
                className={`w-2 h-2 rounded-full ${
                  archived
                    ? "bg-cc-muted opacity-40"
                    : !(s.isConnected || s.backendType === "terminal")
                    ? "bg-cc-muted opacity-40"
                    : (isWaiting || permCount > 0)
                    ? "bg-cc-warning"
                    : isRunning
                    ? "bg-cc-success"
                    : isCompacting
                    ? "bg-cc-warning"
                    : "bg-cc-success opacity-60"
                }`}
              />
              {!archived && (isWaiting || permCount > 0) && s.isConnected && (
                <span className="absolute inset-0 w-2 h-2 rounded-full bg-cc-warning/40 animate-[pulse-dot_1.5s_ease-in-out_infinite]" />
              )}
              {!archived && !(isWaiting || permCount > 0) && isRunning && s.isConnected && (
                <span className="absolute inset-0 w-2 h-2 rounded-full bg-cc-success/40 animate-[pulse-dot_1.5s_ease-in-out_infinite]" />
              )}
            </span>
            {isEditing ? (
              <input
                ref={editInputRef}
                value={editingName}
                onChange={(e) => setEditingName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    confirmRename();
                  } else if (e.key === "Escape") {
                    e.preventDefault();
                    cancelRename();
                  }
                  e.stopPropagation();
                }}
                onBlur={confirmRename}
                onClick={(e) => e.stopPropagation()}
                onDoubleClick={(e) => e.stopPropagation()}
                className="text-[13px] font-medium flex-1 text-cc-fg bg-transparent border border-cc-border rounded-md px-1 py-0 outline-none focus:border-cc-primary/50 min-w-0"
              />
            ) : (
              <span
                className={`text-[13px] font-medium truncate flex-1 text-cc-fg ${recentlyRenamed.has(s.id) ? "animate-name-appear" : ""} flex items-center gap-1.5`}
                onAnimationEnd={() => useStore.getState().clearRecentlyRenamed(s.id)}
              >
                <span className="truncate">{label}</span>
                {s.isRing0 && (
                  <span className="text-[9px] px-1 py-0.5 rounded bg-purple-500/15 text-purple-600 dark:text-purple-400 shrink-0">R0</span>
                )}
                {s.backendType === "codex" && (
                  <span className="text-[9px] px-1 py-0.5 rounded bg-purple-500/15 text-purple-600 dark:text-purple-400 shrink-0">codex</span>
                )}
                {s.backendType === "computer-use" && (
                  <span className="text-[9px] px-1 py-0.5 rounded bg-orange-500/15 text-orange-600 dark:text-orange-400 shrink-0">desktop</span>
                )}
                {s.backendType === "terminal" && (
                  <span className="text-[9px] px-1 py-0.5 rounded bg-green-500/15 text-green-600 dark:text-green-400 shrink-0">term</span>
                )}
                {s.controlledBy === "user" && !s.isRing0 && (
                  <button
                    className="px-1 py-0.5 rounded bg-cc-muted/10 text-cc-muted shrink-0 hover:bg-cc-muted/20 transition-colors cursor-pointer"
                    title="User has the pen — click to release to Ring0"
                    onClick={(e) => {
                      e.stopPropagation();
                      api.setPen(s.id, "ring0");
                    }}
                  >
                    <svg viewBox="0 0 16 16" fill="currentColor" className="w-3 h-3">
                      <path d="M11.5 1.5a1.5 1.5 0 0 1 2.12 0l.88.88a1.5 1.5 0 0 1 0 2.12L5.62 13.38a1 1 0 0 1-.47.26l-3 .75a.5.5 0 0 1-.6-.6l.75-3a1 1 0 0 1 .26-.47L11.5 1.5z" />
                    </svg>
                  </button>
                )}
              </span>
            )}
          </div>
          {dirName && (
            <p className="text-[11px] text-cc-muted truncate mt-0.5 ml-4" title={s.cwd}>
              {dirName}
            </p>
          )}
          {s.gitBranch && (
            <div className="flex items-center gap-1.5 mt-0.5 ml-4 text-[11px] text-cc-muted">
              <span className="flex items-center gap-1 truncate">
                {s.isWorktree ? (
                  <svg viewBox="0 0 16 16" fill="currentColor" className="w-3 h-3 shrink-0 opacity-60">
                    <path d="M5 3.25a.75.75 0 11-1.5 0 .75.75 0 011.5 0zm0 2.122a2.25 2.25 0 10-1.5 0v5.256a2.25 2.25 0 101.5 0V5.372zM4.25 12a.75.75 0 100 1.5.75.75 0 000-1.5zm7.5-9.5a.75.75 0 100 1.5.75.75 0 000-1.5zm-2.25.75a2.25 2.25 0 113 2.122V7A2.5 2.5 0 0110 9.5H6a1 1 0 000 2h4a2.5 2.5 0 012.5 2.5v.628a2.25 2.25 0 11-1.5 0V14a1 1 0 00-1-1H6a2.5 2.5 0 01-2.5-2.5V10a2.5 2.5 0 012.5-2.5h4a1 1 0 001-1V5.372a2.25 2.25 0 01-1.5-2.122z" />
                  </svg>
                ) : (
                  <svg viewBox="0 0 16 16" fill="currentColor" className="w-3 h-3 shrink-0 opacity-60">
                    <path d="M11.75 2.5a.75.75 0 100 1.5.75.75 0 000-1.5zm-2.116.862a2.25 2.25 0 10-.862.862A4.48 4.48 0 007.25 7.5h-1.5A2.25 2.25 0 003.5 9.75v.318a2.25 2.25 0 101.5 0V9.75a.75.75 0 01.75-.75h1.5a5.98 5.98 0 003.884-1.435A2.25 2.25 0 109.634 3.362zM4.25 12a.75.75 0 100 1.5.75.75 0 000-1.5z" />
                  </svg>
                )}
                <span className="truncate">{s.gitBranch}</span>
                {s.isWorktree && (
                  <span className="text-[9px] bg-cc-primary/10 text-cc-primary px-0.5 rounded">wt</span>
                )}
              </span>
              {(s.gitAhead > 0 || s.gitBehind > 0) && (
                <span className="flex items-center gap-0.5 text-[10px]">
                  {s.gitAhead > 0 && <span className="text-green-500">{s.gitAhead}&#8593;</span>}
                  {s.gitBehind > 0 && <span className="text-cc-warning">{s.gitBehind}&#8595;</span>}
                </span>
              )}
              {(s.linesAdded > 0 || s.linesRemoved > 0) && (
                <span className="flex items-center gap-1 shrink-0">
                  <span className="text-green-500">+{s.linesAdded}</span>
                  <span className="text-red-400">-{s.linesRemoved}</span>
                </span>
              )}
            </div>
          )}
        </button>
        {!archived && permCount > 0 && (
          <span className="absolute right-2 top-1/2 -translate-y-1/2 min-w-[18px] h-[18px] flex items-center justify-center rounded-full bg-cc-warning text-white text-[10px] font-bold leading-none px-1 transition-opacity pointer-events-none">
            {permCount}
          </span>
        )}
        {archived ? (
          <>
            {/* Unarchive button */}
            <button
              onClick={(e) => handleUnarchiveSession(e, s.id)}
              className="absolute right-8 top-1/2 -translate-y-1/2 p-1 rounded-md hover:bg-cc-border text-cc-muted hover:text-cc-fg transition-all cursor-pointer"
              title="Restore session"
            >
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-3.5 h-3.5">
                <path d="M8 10V3M5 5l3-3 3 3" strokeLinecap="round" strokeLinejoin="round" />
                <path d="M3 13h10" strokeLinecap="round" />
              </svg>
            </button>
            {/* Delete button */}
            <button
              onClick={(e) => handleDeleteSession(e, s.id)}
              className="absolute right-2 top-1/2 -translate-y-1/2 p-1 rounded-md hover:bg-cc-border text-cc-muted hover:text-red-400 transition-all cursor-pointer"
              title="Delete permanently"
            >
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-3.5 h-3.5">
                <path d="M4 4l8 8M12 4l-8 8" />
              </svg>
            </button>
          </>
        ) : s.backendType === "terminal" ? (
          <button
            onClick={(e) => handleCloseTerminal(e, s.id)}
            className="absolute right-2 top-1/2 -translate-y-1/2 p-1 rounded-md hover:bg-cc-border text-cc-muted hover:text-red-400 transition-all cursor-pointer"
            title="Close terminal"
          >
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-3.5 h-3.5">
              <path d="M4 4l8 8M12 4l-8 8" />
            </svg>
          </button>
        ) : (
          <button
            onClick={(e) => handleArchiveSession(e, s.id)}
            className="absolute right-2 top-1/2 -translate-y-1/2 p-1 rounded-md hover:bg-cc-border text-cc-muted hover:text-cc-fg transition-all cursor-pointer"
            title="Archive session"
          >
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-3.5 h-3.5">
              <path d="M3 3h10v2H3zM4 5v7a1 1 0 001 1h6a1 1 0 001-1V5" strokeLinecap="round" strokeLinejoin="round" />
              <path d="M6.5 8h3" strokeLinecap="round" />
            </svg>
          </button>
        )}
      </div>
    );
  }

  return (
    <aside aria-label="Sessions" className="w-[260px] h-full flex flex-col bg-cc-sidebar border-r border-cc-border">
      {/* Header */}
      <div className="p-4 pb-3">
        <div className="flex items-center gap-2">
          <img src={logoSrc} alt="" className="h-[2.2em] w-auto" />
          <span className="text-2xl font-semibold text-cc-fg tracking-tight">vibr8</span>
        </div>
      </div>

      {/* Worktree archive confirmation */}
      {confirmArchiveId && (
        <div className="mx-2 mb-1 p-2.5 rounded-[10px] bg-amber-500/10 border border-amber-500/20">
          <div className="flex items-start gap-2">
            <svg viewBox="0 0 16 16" fill="currentColor" className="w-4 h-4 text-amber-500 shrink-0 mt-0.5">
              <path d="M8.982 1.566a1.13 1.13 0 00-1.96 0L.165 13.233c-.457.778.091 1.767.98 1.767h13.713c.889 0 1.438-.99.98-1.767L8.982 1.566zM8 5c.535 0 .954.462.9.995l-.35 3.507a.552.552 0 01-1.1 0L7.1 5.995A.905.905 0 018 5zm.002 6a1 1 0 110 2 1 1 0 010-2z" />
            </svg>
            <div className="flex-1 min-w-0">
              <p className="text-[11px] text-cc-fg leading-snug">
                Archiving will <strong>delete the worktree</strong> and any uncommitted changes.
              </p>
              <div className="flex gap-2 mt-2">
                <button
                  onClick={cancelArchive}
                  className="px-2.5 py-1 text-[11px] font-medium rounded-md bg-cc-hover text-cc-muted hover:text-cc-fg transition-colors cursor-pointer"
                >
                  Cancel
                </button>
                <button
                  onClick={confirmArchive}
                  className="px-2.5 py-1 text-[11px] font-medium rounded-md bg-red-500/10 text-red-500 hover:bg-red-500/20 transition-colors cursor-pointer"
                >
                  Archive
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Session list */}
      <div className="flex-1 overflow-hidden relative">
        <div ref={sidebarListRef} className="h-full overflow-y-auto px-2 pb-14" data-session-list>
          {/* Node switcher — only shown when remote nodes or android devices exist */}
          {(nodes.length > 1 || androidDevices.length > 0) && (
            <div className="px-3 pt-2 pb-1">
              <select
                value={activeNodeId}
                onChange={(e) => handleNodeSwitch(e.target.value)}
                className="w-full px-2 py-1.5 text-xs rounded-lg bg-cc-bg border border-cc-border text-cc-fg cursor-pointer focus:outline-none focus:ring-1 focus:ring-cc-primary"
              >
                {nodes.map((n) => (
                  <option key={n.id} value={n.id} disabled={n.status === "offline"}>
                    {n.name} {n.status === "offline" ? "(offline)" : ""}
                  </option>
                ))}
                {androidDevices.length > 0 && (
                  <optgroup label="Android">
                    {androidDevices.map((d) => (
                      <option key={d.id} value={d.id} disabled={d.status !== "online"}>
                        {d.name} {d.status !== "online" ? `(${d.status})` : ""}
                      </option>
                    ))}
                  </optgroup>
                )}
              </select>
            </div>
          )}
          <div className="px-3 pt-1 pb-1.5 text-[10px] uppercase tracking-wider text-cc-muted font-medium">
            Sessions
          </div>
          {activeSessions.length === 0 && archivedSessions.length === 0 ? (
            <p className="px-3 py-8 text-xs text-cc-muted text-center leading-relaxed">
              No sessions yet.
            </p>
          ) : (
            <>
              <div className="space-y-0.5">
                {activeSessions.map((s) => (
                  <Fragment key={s.id}>
                    {renderSessionItem(s)}
                    {/* Desktop entry right after Ring0 */}
                    {s.isRing0 && <DesktopEntry
                      active={activeView === "desktop"}
                      streamActive={desktopStreamActive}
                      splitActive={splitViewActive}
                      onNavigate={() => {
                        useStore.getState().setActiveView("desktop");
                        if (window.innerWidth < 768) useStore.getState().setSidebarOpen(false);
                      }}
                      onStart={async () => {
                        useStore.getState().setActiveView("desktop");
                        if (window.innerWidth < 768) useStore.getState().setSidebarOpen(false);
                        try {
                          await startWebRTC({ desktop: true });
                        } catch (err) {
                          console.error("[desktop] Failed to start:", err);
                        }
                      }}
                      onStop={() => stopDesktopStream()}
                      onToggleSplit={async () => {
                        const st = useStore.getState();
                        const willEnable = !st.splitViewActive;
                        st.toggleSplitView();
                        if (willEnable) {
                          st.setActiveView("session");
                          if (!st.desktopStreamActive) {
                            try { await startWebRTC({ desktop: true }); } catch (err) { console.error("[desktop] Failed to start:", err); }
                          }
                        }
                      }}
                    />}
                  </Fragment>
                ))}
                {/* If no Ring0 session, still show Desktop entry */}
                {!activeSessions.some((s) => s.isRing0) && (
                  <DesktopEntry
                    active={activeView === "desktop"}
                    streamActive={desktopStreamActive}
                    splitActive={splitViewActive}
                    onNavigate={() => {
                      useStore.getState().setActiveView("desktop");
                      if (window.innerWidth < 768) useStore.getState().setSidebarOpen(false);
                    }}
                    onStart={async () => {
                      useStore.getState().setActiveView("desktop");
                      if (window.innerWidth < 768) useStore.getState().setSidebarOpen(false);
                      try {
                        await startWebRTC({ desktop: true });
                      } catch (err) {
                        console.error("[desktop] Failed to start:", err);
                      }
                    }}
                    onStop={() => stopDesktopStream()}
                    onToggleSplit={async () => {
                      const st = useStore.getState();
                      const willEnable = !st.splitViewActive;
                      st.toggleSplitView();
                      if (willEnable) {
                        st.setActiveView("session");
                        if (!st.desktopStreamActive) {
                          try { await startWebRTC({ desktop: true }); } catch (err) { console.error("[desktop] Failed to start:", err); }
                        }
                      }
                    }}
                  />
                )}
              </div>

              {archivedSessions.length > 0 && (
                <div className="mt-2 pt-2 border-t border-cc-border">
                  <button
                    onClick={() => setShowArchived(!showArchived)}
                    className="w-full px-3 py-1.5 text-[11px] font-medium text-cc-muted uppercase tracking-wider flex items-center gap-1.5 hover:text-cc-fg transition-colors cursor-pointer"
                  >
                    <svg viewBox="0 0 16 16" fill="currentColor" className={`w-3 h-3 transition-transform ${showArchived ? "rotate-90" : ""}`}>
                      <path d="M6 4l4 4-4 4" />
                    </svg>
                    Archived ({archivedSessions.length})
                  </button>
                  {showArchived && (
                    <div className="space-y-0.5 mt-1">
                      {archivedSessions.map((s) => renderSessionItem(s, { isArchived: true }))}
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </div>

        {/* Floating new session button */}
        <button
          ref={newSessionRef}
          onClick={handleNewSession}
          className="absolute bottom-3 right-3 w-10 h-10 rounded-full bg-cc-primary hover:bg-cc-primary-hover text-white shadow-lg flex items-center justify-center transition-colors cursor-pointer z-10"
          title="New Session"
        >
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2.5" className="w-4 h-4">
            <path d="M8 3v10M3 8h10" strokeLinecap="round" />
          </svg>
        </button>
      </div>

      {/* Footer */}
      <div className="p-3 border-t border-cc-border space-y-0.5">
        <button
          onClick={() => { window.location.hash = "#/settings"; }}
          className="w-full flex items-center gap-2.5 px-3 py-2 rounded-[10px] text-sm text-cc-muted hover:text-cc-fg hover:bg-cc-hover transition-colors cursor-pointer"
        >
          <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
            <path fillRule="evenodd" d="M11.49 3.17c-.38-1.56-2.6-1.56-2.98 0a1.532 1.532 0 01-2.286.948c-1.372-.836-2.942.734-2.106 2.106.54.886.061 2.042-.947 2.287-1.561.379-1.561 2.6 0 2.978a1.532 1.532 0 01.947 2.287c-.836 1.372.734 2.942 2.106 2.106a1.532 1.532 0 012.287.947c.379 1.561 2.6 1.561 2.978 0a1.533 1.533 0 012.287-.947c1.372.836 2.942-.734 2.106-2.106a1.533 1.533 0 01.947-2.287c1.561-.379 1.561-2.6 0-2.978a1.532 1.532 0 01-.947-2.287c.836-1.372-.734-2.942-2.106-2.106a1.532 1.532 0 01-2.287-.947zM10 13a3 3 0 100-6 3 3 0 000 6z" clipRule="evenodd" />
          </svg>
          <span>Settings</span>
        </button>
        <button
          onClick={() => setShowEnvManager(true)}
          className="w-full flex items-center gap-2.5 px-3 py-2 rounded-[10px] text-sm text-cc-muted hover:text-cc-fg hover:bg-cc-hover transition-colors cursor-pointer"
        >
          <svg viewBox="0 0 16 16" fill="currentColor" className="w-4 h-4">
            <path d="M8 1a2 2 0 012 2v1h2a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6a2 2 0 012-2h2V3a2 2 0 012-2zm0 1.5a.5.5 0 00-.5.5v1h1V3a.5.5 0 00-.5-.5zM4 5.5a.5.5 0 00-.5.5v6a.5.5 0 00.5.5h8a.5.5 0 00.5-.5V6a.5.5 0 00-.5-.5H4z" />
          </svg>
          <span>Environments</span>
        </button>
        {ring0SessionId && (
          <button
            onClick={async () => {
              const newMuted = !ring0EventsMuted;
              setRing0EventsMuted(newMuted);
              await api.muteRing0Events(newMuted).catch(() => setRing0EventsMuted(!newMuted));
            }}
            className="w-full flex items-center gap-2.5 px-3 py-2 rounded-[10px] text-sm text-cc-muted hover:text-cc-fg hover:bg-cc-hover transition-colors cursor-pointer"
          >
            {ring0EventsMuted ? (
              <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
                <path fillRule="evenodd" d="M5.05 3.636a1 1 0 010 1.414 7 7 0 000 9.9 1 1 0 11-1.414 1.414 9 9 0 010-12.728 1 1 0 011.414 0zm9.9 0a1 1 0 011.414 0 9 9 0 010 12.728 1 1 0 11-1.414-1.414 7 7 0 000-9.9 1 1 0 010-1.414zM7.879 6.464a1 1 0 010 1.414 3 3 0 000 4.243 1 1 0 11-1.415 1.414 5 5 0 010-7.07 1 1 0 011.415 0zm4.242 0a1 1 0 011.415 0 5 5 0 010 7.072 1 1 0 01-1.415-1.415 3 3 0 000-4.242 1 1 0 010-1.415zM10 9a1 1 0 100 2 1 1 0 000-2z" clipRule="evenodd" />
              </svg>
            ) : (
              <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
                <path fillRule="evenodd" d="M5.05 3.636a1 1 0 010 1.414 7 7 0 000 9.9 1 1 0 11-1.414 1.414 9 9 0 010-12.728 1 1 0 011.414 0zm9.9 0a1 1 0 011.414 0 9 9 0 010 12.728 1 1 0 11-1.414-1.414 7 7 0 000-9.9 1 1 0 010-1.414zM7.879 6.464a1 1 0 010 1.414 3 3 0 000 4.243 1 1 0 11-1.415 1.414 5 5 0 010-7.07 1 1 0 011.415 0zm4.242 0a1 1 0 011.415 0 5 5 0 010 7.072 1 1 0 01-1.415-1.415 3 3 0 000-4.242 1 1 0 010-1.415zM10 9a1 1 0 100 2 1 1 0 000-2z" clipRule="evenodd" />
              </svg>
            )}
            <span>{ring0EventsMuted ? "Events muted" : "Events on"}</span>
          </button>
        )}
        <button
          onClick={toggleNotificationSound}
          className="w-full flex items-center gap-2.5 px-3 py-2 rounded-[10px] text-sm text-cc-muted hover:text-cc-fg hover:bg-cc-hover transition-colors cursor-pointer"
        >
          {notificationSound ? (
            <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
              <path d="M9.383 3.076A1 1 0 0110 4v12a1 1 0 01-1.707.707L4.586 13H2a1 1 0 01-1-1V8a1 1 0 011-1h2.586l3.707-3.707a1 1 0 011.09-.217zM14.657 2.929a1 1 0 011.414 0A9.972 9.972 0 0119 10a9.972 9.972 0 01-2.929 7.071 1 1 0 01-1.414-1.414A7.971 7.971 0 0017 10c0-2.21-.894-4.208-2.343-5.657a1 1 0 010-1.414zm-2.829 2.828a1 1 0 011.415 0A5.983 5.983 0 0115 10a5.984 5.984 0 01-1.757 4.243 1 1 0 01-1.415-1.415A3.984 3.984 0 0013 10a3.983 3.983 0 00-1.172-2.828 1 1 0 010-1.415z" />
            </svg>
          ) : (
            <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
              <path fillRule="evenodd" d="M9.383 3.076A1 1 0 0110 4v12a1 1 0 01-1.707.707L4.586 13H2a1 1 0 01-1-1V8a1 1 0 011-1h2.586l3.707-3.707a1 1 0 011.09-.217zM12.293 7.293a1 1 0 011.414 0L15 8.586l1.293-1.293a1 1 0 111.414 1.414L16.414 10l1.293 1.293a1 1 0 01-1.414 1.414L15 11.414l-1.293 1.293a1 1 0 01-1.414-1.414L13.586 10l-1.293-1.293a1 1 0 010-1.414z" clipRule="evenodd" />
            </svg>
          )}
          <span>{notificationSound ? "Sound on" : "Sound off"}</span>
        </button>
        <button
          onClick={toggleDarkMode}
          className="w-full flex items-center gap-2.5 px-3 py-2 rounded-[10px] text-sm text-cc-muted hover:text-cc-fg hover:bg-cc-hover transition-colors cursor-pointer"
        >
          {darkMode ? (
            <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
              <path fillRule="evenodd" d="M10 2a1 1 0 011 1v1a1 1 0 11-2 0V3a1 1 0 011-1zm4 8a4 4 0 11-8 0 4 4 0 018 0zm-.464 4.95l.707.707a1 1 0 001.414-1.414l-.707-.707a1 1 0 00-1.414 1.414zm2.12-10.607a1 1 0 010 1.414l-.706.707a1 1 0 11-1.414-1.414l.707-.707a1 1 0 011.414 0zM17 11a1 1 0 100-2h-1a1 1 0 100 2h1zm-7 4a1 1 0 011 1v1a1 1 0 11-2 0v-1a1 1 0 011-1zM5.05 6.464A1 1 0 106.465 5.05l-.708-.707a1 1 0 00-1.414 1.414l.707.707zm1.414 8.486l-.707.707a1 1 0 01-1.414-1.414l.707-.707a1 1 0 011.414 1.414zM4 11a1 1 0 100-2H3a1 1 0 000 2h1z" clipRule="evenodd" />
            </svg>
          ) : (
            <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
              <path d="M17.293 13.293A8 8 0 016.707 2.707a8.001 8.001 0 1010.586 10.586z" />
            </svg>
          )}
          <span>{darkMode ? "Light mode" : "Dark mode"}</span>
        </button>
        {import.meta.env.DEV && (
          <button
            onClick={async () => {
              try {
                await api.restartServer();
              } catch {
                // Server may close connection before response completes
              }
            }}
            className="w-full flex items-center gap-2.5 px-3 py-2 rounded-[10px] text-sm text-cc-muted hover:text-amber-500 hover:bg-cc-hover transition-colors cursor-pointer"
          >
            <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
              <path fillRule="evenodd" d="M4 2a1 1 0 011 1v2.101a7.002 7.002 0 0111.601 2.566 1 1 0 11-1.885.666A5.002 5.002 0 005.999 7H9a1 1 0 010 2H4a1 1 0 01-1-1V3a1 1 0 011-1zm.008 9.057a1 1 0 011.276.61A5.002 5.002 0 0014.001 13H11a1 1 0 110-2h5a1 1 0 011 1v5a1 1 0 11-2 0v-2.101a7.002 7.002 0 01-11.601-2.566 1 1 0 01.61-1.276z" clipRule="evenodd" />
            </svg>
            <span>Restart Server</span>
          </button>
        )}
        {authEnabled && (
          <button
            onClick={() => {
              fetch("/api/auth/logout", { method: "POST" }).finally(() => {
                window.location.reload();
              });
            }}
            className="w-full flex items-center gap-2.5 px-3 py-2 rounded-[10px] text-sm text-cc-muted hover:text-red-400 hover:bg-cc-hover transition-colors cursor-pointer"
          >
            <svg viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4">
              <path fillRule="evenodd" d="M3 3a1 1 0 00-1 1v12a1 1 0 001 1h5a1 1 0 100-2H4V5h4a1 1 0 100-2H3zm11.293 3.293a1 1 0 011.414 0l3 3a1 1 0 010 1.414l-3 3a1 1 0 01-1.414-1.414L15.586 11H8a1 1 0 110-2h7.586l-1.293-1.293a1 1 0 010-1.414z" clipRule="evenodd" />
            </svg>
            <span>Sign out</span>
          </button>
        )}
      </div>

      {/* Environment manager modal */}
      {showEnvManager && (
        <EnvManager onClose={() => setShowEnvManager(false)} />
      )}
    </aside>
  );
}

// ── Desktop pseudo-session entry ─────────────────────────────────────────────

function DesktopEntry({
  active,
  streamActive,
  splitActive,
  onNavigate,
  onStart,
  onStop,
  onToggleSplit,
}: {
  active: boolean;
  streamActive: boolean;
  splitActive: boolean;
  onNavigate: () => void;
  onStart: () => void;
  onStop: () => void;
  onToggleSplit: () => void;
}) {
  return (
    <div className="relative group">
      <button
        onClick={() => {
          if (streamActive) {
            onNavigate();
          } else {
            onStart();
          }
        }}
        className={`w-full px-3 py-2.5 pr-8 text-left rounded-[10px] transition-all duration-100 cursor-pointer ${
          active || splitActive
            ? "bg-cc-active"
            : "hover:bg-cc-hover"
        }`}
      >
        <div className="flex items-center gap-2">
          <span className="relative flex shrink-0">
            <span
              className={`w-2 h-2 rounded-full ${
                streamActive ? "bg-cc-success opacity-60" : "bg-cc-muted opacity-40"
              }`}
            />
          </span>
          <span className="text-[13px] font-medium truncate flex-1 text-cc-fg flex items-center gap-1.5">
            {/* Monitor icon */}
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" className="w-3.5 h-3.5 shrink-0 opacity-60">
              <rect x="2" y="3" width="12" height="8" rx="1" />
              <path d="M5 14h6M8 11v3" />
            </svg>
            <span className="truncate">Desktop</span>
          </span>
        </div>
      </button>
      {/* Action buttons */}
      <div className="absolute right-1.5 top-1/2 -translate-y-1/2 flex items-center gap-0.5">
        {/* Split view toggle (hidden on mobile) — always visible */}
        <button
          onClick={(e) => {
            e.stopPropagation();
            onToggleSplit();
          }}
          className={`flex p-1 rounded-md transition-all cursor-pointer ${
            splitActive
              ? "bg-cc-primary/20 text-cc-primary"
              : "hover:bg-cc-border text-cc-muted hover:text-cc-fg"
          }`}
          title={splitActive ? "Close split view" : "Split view"}
        >
          {/* Horizontal split icon — two stacked rectangles */}
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-3.5 h-3.5">
            <rect x="2" y="2" width="12" height="5" rx="1" />
            <rect x="2" y="9" width="12" height="5" rx="1" />
          </svg>
        </button>
        {/* Stop button — only when stream is active */}
        {streamActive && (
          <button
            onClick={(e) => {
              e.stopPropagation();
              onStop();
            }}
            className="p-1 rounded-md hover:bg-cc-border text-cc-muted hover:text-red-400 transition-all cursor-pointer"
            title="Stop desktop stream"
          >
            <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-3.5 h-3.5">
              <path d="M4 4l8 8M12 4l-8 8" strokeLinecap="round" />
            </svg>
          </button>
        )}
      </div>
    </div>
  );
}
