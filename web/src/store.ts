import { create } from "zustand";
import type { SessionState, PermissionRequest, ChatMessage, SdkSessionInfo, TaskItem, NodeInfo } from "./types.js";

function getClientId(): string {
  if (typeof window === "undefined") return crypto.randomUUID();
  const key = "vibr8_client_id";
  let id = localStorage.getItem(key);
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem(key, id);
  }
  return id;
}

interface AppState {
  // Per-app client identity
  clientId: string;
  clientRole: "primary" | "secondscreen";

  // Sessions
  sessions: Map<string, SessionState>;
  sdkSessions: SdkSessionInfo[];
  currentSessionId: string | null;
  sessionsLoaded: boolean;

  // Messages per session
  messages: Map<string, ChatMessage[]>;
  archivedCount: Map<string, number>;

  // Streaming partial text per session
  streaming: Map<string, string>;

  // Streaming stats: start time + output tokens
  streamingStartedAt: Map<string, number>;
  streamingOutputTokens: Map<string, number>;

  // Pending permissions per session (outer key = sessionId, inner key = request_id)
  pendingPermissions: Map<string, Map<string, PermissionRequest>>;

  // Connection state per session
  connectionStatus: Map<string, "connecting" | "connected" | "disconnected">;
  cliConnected: Map<string, boolean>;

  // Session status
  sessionStatus: Map<string, "idle" | "running" | "compacting" | "waiting_for_permission" | "watching" | "confirming" | "paused" | null>;

  // Plan mode: stores previous permission mode per session so we can restore it
  previousPermissionMode: Map<string, string>;

  // Tasks per session
  sessionTasks: Map<string, TaskItem[]>;

  // Files changed by the agent per session (Edit/Write tool calls)
  changedFiles: Map<string, Set<string>>;

  // Session display names
  sessionNames: Map<string, string>;
  // Track sessions that were just renamed (for animation)
  recentlyRenamed: Set<string>;

  // UI
  darkMode: boolean;
  notificationSound: boolean;
  sidebarOpen: boolean;
  taskPanelOpen: boolean;
  homeResetKey: number;
  activeTab: "chat" | "editor" | "terminal";
  activeView: "session" | "desktop";
  splitViewActive: boolean;

  // Computer-use agent controls
  agentMode: "watch" | "act";
  executionMode: "auto" | "confirm" | "gated";
  pendingConfirmation: { step: number; actionType: string; actionSummary: string; thought: string } | null;

  editorOpenFile: Map<string, string>;
  editorUrl: Map<string, string>;
  editorLoading: Map<string, boolean>;

  // Reconnect state per session
  reconnecting: Map<string, boolean>;
  reconnectGaveUp: Map<string, boolean>;

  // WebRTC audio state (global singleton — one audio connection per client)
  audioActive: boolean;
  audioMode: "off" | "connecting" | "in_out" | "in_only";
  isRecording: boolean;
  webrtcStatus: string | null;
  webrtcTransport: "direct" | "relay" | null;
  guardEnabled: boolean;
  voiceMode: string | null;
  activeAudioInputLabel: string | null;
  desktopStreamActive: boolean;
  desktopRemoteStream: MediaStream | null;
  desktopStatus: "idle" | "connecting" | "connected" | "reconnecting" | "offline";
  desktopStats: { fps: number; bitrate: number; rtt: number } | null;

  // Focus management
  pendingFocus: "composer" | "terminal" | "home" | null;

  // Command palette
  commandPaletteOpen: boolean;

  // Second screen pushed content
  secondScreenContent: { type: string; content: string; filename?: string; nodeId?: string } | null;
  mirroredSessionId: string | null;
  secondScreenScale: number;
  secondScreenTvSafe: number; // 0 = off, >0 = padding percent
  secondScreenClientName: string | null;
  secondScreenDarkMode: boolean;

  // Remote nodes
  nodes: NodeInfo[];
  activeNodeId: string;

  // Playground state
  playgroundActive: boolean;
  playgroundSessionId: string | null;
  playgroundSegments: { transcript: string; timeBegin: number; timeEnd: number; timestamp: number; segmentId?: string }[];
  playgroundRmsDb: number;
  playgroundVadActive: boolean;

  // Actions
  setClientRole: (role: "primary" | "secondscreen") => void;
  setSecondScreenContent: (content: { type: string; content: string; filename?: string; nodeId?: string } | null) => void;
  setMirroredSessionId: (id: string | null) => void;
  setSecondScreenScale: (scale: number) => void;
  setSecondScreenTvSafe: (padding: number) => void;
  setSecondScreenClientName: (name: string | null) => void;
  setSecondScreenDarkMode: (v: boolean) => void;
  setDarkMode: (v: boolean) => void;
  toggleDarkMode: () => void;
  setNotificationSound: (v: boolean) => void;
  toggleNotificationSound: () => void;
  setSidebarOpen: (v: boolean) => void;
  setTaskPanelOpen: (open: boolean) => void;
  newSession: () => void;

  // Session actions
  setCurrentSession: (id: string | null) => void;
  addSession: (session: SessionState) => void;
  updateSession: (sessionId: string, updates: Partial<SessionState>) => void;
  removeSession: (sessionId: string) => void;
  setSdkSessions: (sessions: SdkSessionInfo[]) => void;

  // Message actions
  appendMessage: (sessionId: string, msg: ChatMessage) => void;
  setMessages: (sessionId: string, msgs: ChatMessage[]) => void;
  setArchivedCount: (sessionId: string, count: number) => void;
  updateLastAssistantMessage: (sessionId: string, updater: (msg: ChatMessage) => ChatMessage) => void;
  setStreaming: (sessionId: string, text: string | null) => void;
  setStreamingStats: (sessionId: string, stats: { startedAt?: number; outputTokens?: number } | null) => void;

  // Permission actions
  addPermission: (sessionId: string, perm: PermissionRequest) => void;
  removePermission: (sessionId: string, requestId: string) => void;

  // Task actions
  addTask: (sessionId: string, task: TaskItem) => void;
  setTasks: (sessionId: string, tasks: TaskItem[]) => void;
  updateTask: (sessionId: string, taskId: string, updates: Partial<TaskItem>) => void;

  // Changed files actions
  addChangedFile: (sessionId: string, filePath: string) => void;
  clearChangedFiles: (sessionId: string) => void;

  // Session name actions
  setSessionName: (sessionId: string, name: string) => void;
  markRecentlyRenamed: (sessionId: string) => void;
  clearRecentlyRenamed: (sessionId: string) => void;

  // Plan mode actions
  setPreviousPermissionMode: (sessionId: string, mode: string) => void;

  // Connection actions
  setConnectionStatus: (sessionId: string, status: "connecting" | "connected" | "disconnected") => void;
  setCliConnected: (sessionId: string, connected: boolean) => void;
  setSessionStatus: (sessionId: string, status: "idle" | "running" | "compacting" | "waiting_for_permission" | "watching" | "confirming" | "paused" | null) => void;
  setReconnecting: (sessionId: string, value: boolean) => void;
  setReconnectGaveUp: (sessionId: string, value: boolean) => void;

  // Editor actions
  setActiveTab: (tab: "chat" | "editor") => void;
  setActiveView: (view: "session" | "desktop") => void;
  setSplitViewActive: (active: boolean) => void;
  toggleSplitView: () => void;
  setAgentMode: (mode: "watch" | "act") => void;
  setExecutionMode: (mode: "auto" | "confirm" | "gated") => void;
  setPendingConfirmation: (confirm: { step: number; actionType: string; actionSummary: string; thought: string } | null) => void;
  setEditorOpenFile: (sessionId: string, filePath: string | null) => void;
  setEditorUrl: (sessionId: string, url: string) => void;
  setEditorLoading: (sessionId: string, loading: boolean) => void;

  // WebRTC audio actions (global)
  setAudioActive: (active: boolean) => void;
  setAudioMode: (mode: "off" | "connecting" | "in_out" | "in_only") => void;
  setIsRecording: (recording: boolean) => void;
  setWebRTCStatus: (status: string | null) => void;
  setWebRTCTransport: (transport: "direct" | "relay" | null) => void;
  setDesktopStreamActive: (active: boolean) => void;
  setDesktopRemoteStream: (stream: MediaStream | null) => void;
  setDesktopStatus: (status: "idle" | "connecting" | "connected" | "reconnecting" | "offline") => void;
  setDesktopStats: (stats: { fps: number; bitrate: number; rtt: number } | null) => void;

  // Focus management actions
  setPendingFocus: (target: "composer" | "terminal" | "home" | null) => void;

  // Command palette actions
  setCommandPaletteOpen: (open: boolean) => void;
  toggleCommandPalette: () => void;

  // Guard mode actions (global)
  setGuardEnabled: (enabled: boolean) => void;
  setVoiceMode: (mode: string | null) => void;
  setActiveAudioInputLabel: (label: string | null) => void;

  // Node actions
  setNodes: (nodes: NodeInfo[]) => void;
  setActiveNode: (nodeId: string) => void;
  clearSessionState: () => void;

  // Playground actions
  setPlaygroundActive: (active: boolean) => void;
  setPlaygroundSessionId: (id: string | null) => void;
  addPlaygroundSegment: (segment: { transcript: string; timeBegin: number; timeEnd: number; segmentId?: string }) => void;
  clearPlaygroundSegments: () => void;
  setPlaygroundLevel: (rmsDb: number) => void;
  setPlaygroundVadActive: (active: boolean) => void;

  reset: () => void;
}

function getInitialDarkMode(): boolean {
  if (typeof window === "undefined") return false;
  const stored = localStorage.getItem("cc-dark-mode");
  if (stored !== null) return stored === "true";
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function getInitialNotificationSound(): boolean {
  if (typeof window === "undefined") return true;
  const stored = localStorage.getItem("cc-notification-sound");
  if (stored !== null) return stored === "true";
  return true;
}

export const useStore = create<AppState>((set) => ({
  clientId: getClientId(),
  clientRole: "primary",
  sessions: new Map(),
  sdkSessions: [],
  currentSessionId: typeof window !== "undefined" ? (localStorage.getItem("cc-node-session-local") ?? localStorage.getItem("cc-last-session")) : null,
  sessionsLoaded: false,
  messages: new Map(),
  archivedCount: new Map(),
  streaming: new Map(),
  streamingStartedAt: new Map(),
  streamingOutputTokens: new Map(),
  pendingPermissions: new Map(),
  connectionStatus: new Map(),
  cliConnected: new Map(),
  sessionStatus: new Map(),
  previousPermissionMode: new Map(),
  sessionTasks: new Map(),
  changedFiles: new Map(),
  sessionNames: new Map(),
  recentlyRenamed: new Set(),
  darkMode: getInitialDarkMode(),
  notificationSound: getInitialNotificationSound(),
  sidebarOpen: typeof window !== "undefined" ? localStorage.getItem("cc-sidebar-open") !== "false" : true,
  taskPanelOpen: typeof window !== "undefined" ? localStorage.getItem("cc-task-panel-open") !== "false" && window.innerWidth >= 1024 : false,
  homeResetKey: 0,
  activeTab: "chat",
  activeView: "session" as const,
  splitViewActive: false,
  agentMode: "watch" as const,
  executionMode: "auto" as const,
  pendingConfirmation: null,
  editorOpenFile: new Map(),
  editorUrl: new Map(),
  editorLoading: new Map(),
  reconnecting: new Map(),
  reconnectGaveUp: new Map(),
  audioActive: false,
  audioMode: "off" as const,
  isRecording: false,
  webrtcStatus: null,
  webrtcTransport: null,
  desktopStreamActive: false,
  desktopRemoteStream: null,
  desktopStatus: "idle" as const,
  desktopStats: null,
  guardEnabled: typeof window !== "undefined" ? localStorage.getItem("cc-guard-enabled") !== "false" : true,
  voiceMode: null,
  activeAudioInputLabel: null,
  pendingFocus: null,
  commandPaletteOpen: false,
  secondScreenContent: (() => {
    const raw = localStorage.getItem("cc-second-screen-content");
    try { return raw ? JSON.parse(raw) : null; } catch { return null; }
  })(),
  mirroredSessionId: null,
  secondScreenScale: parseFloat(localStorage.getItem("cc-second-screen-scale") || "1.5"),
  secondScreenClientName: null,
  secondScreenTvSafe: (() => {
    const v = localStorage.getItem("cc-second-screen-tv-safe");
    if (v === "true") return 2.5; // migrate legacy boolean
    if (v === "false" || v === null) return 0;
    const n = parseFloat(v);
    return isNaN(n) ? 0 : n;
  })(),
  secondScreenDarkMode: localStorage.getItem("cc-second-screen-dark-mode") !== "false",
  nodes: [],
  activeNodeId: "local",
  playgroundActive: false,
  playgroundSessionId: null,
  playgroundSegments: [],
  playgroundRmsDb: -60,
  playgroundVadActive: false,

  setClientRole: (role) => set({ clientRole: role }),
  setSecondScreenContent: (content) => {
    if (content) {
      localStorage.setItem("cc-second-screen-content", JSON.stringify(content));
    } else {
      localStorage.removeItem("cc-second-screen-content");
    }
    set({ secondScreenContent: content });
  },
  setMirroredSessionId: (id) => set({ mirroredSessionId: id }),
  setSecondScreenScale: (scale) => {
    const clamped = Math.max(0.5, Math.min(3.0, scale));
    localStorage.setItem("cc-second-screen-scale", String(clamped));
    set({ secondScreenScale: clamped });
  },
  setSecondScreenTvSafe: (padding) => {
    localStorage.setItem("cc-second-screen-tv-safe", String(padding));
    set({ secondScreenTvSafe: padding });
  },
  setSecondScreenClientName: (name) => set({ secondScreenClientName: name }),
  setSecondScreenDarkMode: (v) => {
    localStorage.setItem("cc-second-screen-dark-mode", String(v));
    set({ secondScreenDarkMode: v });
  },
  setDarkMode: (v) => {
    localStorage.setItem("cc-dark-mode", String(v));
    set({ darkMode: v });
  },
  toggleDarkMode: () =>
    set((s) => {
      const next = !s.darkMode;
      localStorage.setItem("cc-dark-mode", String(next));
      return { darkMode: next };
    }),
  setNotificationSound: (v) => {
    localStorage.setItem("cc-notification-sound", String(v));
    set({ notificationSound: v });
  },
  toggleNotificationSound: () =>
    set((s) => {
      const next = !s.notificationSound;
      localStorage.setItem("cc-notification-sound", String(next));
      return { notificationSound: next };
    }),
  setSidebarOpen: (v) => {
    localStorage.setItem("cc-sidebar-open", String(v));
    set({ sidebarOpen: v });
  },
  setTaskPanelOpen: (open) => {
    localStorage.setItem("cc-task-panel-open", String(open));
    set({ taskPanelOpen: open });
  },
  newSession: () => {
    set((s) => ({ currentSessionId: null, homeResetKey: s.homeResetKey + 1 }));
  },

  setCurrentSession: (id) => {
    const nodeId = useStore.getState().activeNodeId;
    if (id) {
      localStorage.setItem("cc-last-session", id);
      localStorage.setItem(`cc-node-session-${nodeId}`, id);
    }
    set({ currentSessionId: id });
  },

  addSession: (session) =>
    set((s) => {
      const sessions = new Map(s.sessions);
      sessions.set(session.session_id, session);
      const messages = new Map(s.messages);
      if (!messages.has(session.session_id)) messages.set(session.session_id, []);
      return { sessions, messages };
    }),

  updateSession: (sessionId, updates) =>
    set((s) => {
      const sessions = new Map(s.sessions);
      const existing = sessions.get(sessionId);
      if (existing) sessions.set(sessionId, { ...existing, ...updates });
      return { sessions };
    }),

  removeSession: (sessionId) =>
    set((s) => {
      const sessions = new Map(s.sessions);
      sessions.delete(sessionId);
      const messages = new Map(s.messages);
      messages.delete(sessionId);
      const streaming = new Map(s.streaming);
      streaming.delete(sessionId);
      const streamingStartedAt = new Map(s.streamingStartedAt);
      streamingStartedAt.delete(sessionId);
      const streamingOutputTokens = new Map(s.streamingOutputTokens);
      streamingOutputTokens.delete(sessionId);
      const connectionStatus = new Map(s.connectionStatus);
      connectionStatus.delete(sessionId);
      const cliConnected = new Map(s.cliConnected);
      cliConnected.delete(sessionId);
      const sessionStatus = new Map(s.sessionStatus);
      sessionStatus.delete(sessionId);
      const previousPermissionMode = new Map(s.previousPermissionMode);
      previousPermissionMode.delete(sessionId);
      const pendingPermissions = new Map(s.pendingPermissions);
      pendingPermissions.delete(sessionId);
      const sessionTasks = new Map(s.sessionTasks);
      sessionTasks.delete(sessionId);
      const changedFiles = new Map(s.changedFiles);
      changedFiles.delete(sessionId);
      const sessionNames = new Map(s.sessionNames);
      sessionNames.delete(sessionId);
      const recentlyRenamed = new Set(s.recentlyRenamed);
      recentlyRenamed.delete(sessionId);
      const editorOpenFile = new Map(s.editorOpenFile);
      editorOpenFile.delete(sessionId);
      const editorUrl = new Map(s.editorUrl);
      editorUrl.delete(sessionId);
      const editorLoading = new Map(s.editorLoading);
      editorLoading.delete(sessionId);
      const audioReset = {};
      return {
        sessions,
        messages,
        streaming,
        streamingStartedAt,
        streamingOutputTokens,
        connectionStatus,
        cliConnected,
        sessionStatus,
        previousPermissionMode,
        pendingPermissions,
        sessionTasks,
        changedFiles,
        sessionNames,
        recentlyRenamed,
        editorOpenFile,
        editorUrl,
        editorLoading,
        ...audioReset,
        sdkSessions: s.sdkSessions.filter((sdk) => sdk.sessionId !== sessionId),
        currentSessionId: s.currentSessionId === sessionId ? null : s.currentSessionId,
      };
    }),

  setSdkSessions: (sessions) => {
    set((s) => {
      // Populate sessionNames from server data for any sessions without a name
      const names = new Map(s.sessionNames);
      let namesChanged = false;
      for (const sdk of sessions) {
        if (sdk.name && !names.has(sdk.sessionId)) {
          names.set(sdk.sessionId, sdk.name);
          namesChanged = true;
        }
      }
      // Seed sessionStatus and cliConnected from REST agentState / state
      const sessionStatus = new Map(s.sessionStatus);
      const cliConnected = new Map(s.cliConnected);
      for (const sdk of sessions) {
        if (sdk.agentState) {
          const current = sessionStatus.get(sdk.sessionId);
          // Don't downgrade running→idle from REST (WS is authoritative for active session)
          if (!(current === "running" && sdk.agentState === "idle")) {
            sessionStatus.set(sdk.sessionId, sdk.agentState);
          }
        }
        // Seed cliConnected from SDK process state
        const sdkConnected = sdk.state === "connected" || sdk.state === "running";
        const wsConnected = cliConnected.get(sdk.sessionId);
        // Only seed if WS hasn't already set a value (WS is more real-time)
        if (wsConnected === undefined) {
          cliConnected.set(sdk.sessionId, sdkConnected);
        }
      }
      return {
        sdkSessions: sessions,
        sessionsLoaded: true,
        sessionStatus,
        cliConnected,
        ...(namesChanged ? { sessionNames: names } : {}),
      };
    });
  },

  appendMessage: (sessionId, msg) =>
    set((s) => {
      const existing = s.messages.get(sessionId) || [];
      // Deduplicate by message ID (assistant messages have stable IDs from CLI)
      if (msg.id && existing.some((m) => m.id === msg.id)) return {};
      const messages = new Map(s.messages);
      messages.set(sessionId, [...existing, msg]);
      return { messages };
    }),

  setMessages: (sessionId, msgs) =>
    set((s) => {
      const messages = new Map(s.messages);
      messages.set(sessionId, msgs);
      return { messages };
    }),

  setArchivedCount: (sessionId, count) =>
    set((s) => {
      const archivedCount = new Map(s.archivedCount);
      archivedCount.set(sessionId, count);
      return { archivedCount };
    }),

  updateLastAssistantMessage: (sessionId, updater) =>
    set((s) => {
      const messages = new Map(s.messages);
      const list = [...(messages.get(sessionId) || [])];
      for (let i = list.length - 1; i >= 0; i--) {
        if (list[i].role === "assistant") {
          list[i] = updater(list[i]);
          break;
        }
      }
      messages.set(sessionId, list);
      return { messages };
    }),

  setStreaming: (sessionId, text) =>
    set((s) => {
      const streaming = new Map(s.streaming);
      if (text === null) {
        streaming.delete(sessionId);
      } else {
        streaming.set(sessionId, text);
      }
      return { streaming };
    }),

  setStreamingStats: (sessionId, stats) =>
    set((s) => {
      const streamingStartedAt = new Map(s.streamingStartedAt);
      const streamingOutputTokens = new Map(s.streamingOutputTokens);
      if (stats === null) {
        streamingStartedAt.delete(sessionId);
        streamingOutputTokens.delete(sessionId);
      } else {
        if (stats.startedAt !== undefined) streamingStartedAt.set(sessionId, stats.startedAt);
        if (stats.outputTokens !== undefined) streamingOutputTokens.set(sessionId, stats.outputTokens);
      }
      return { streamingStartedAt, streamingOutputTokens };
    }),

  addPermission: (sessionId, perm) =>
    set((s) => {
      const pendingPermissions = new Map(s.pendingPermissions);
      const sessionPerms = new Map(pendingPermissions.get(sessionId) || []);
      sessionPerms.set(perm.request_id, perm);
      pendingPermissions.set(sessionId, sessionPerms);
      return { pendingPermissions };
    }),

  removePermission: (sessionId, requestId) =>
    set((s) => {
      const pendingPermissions = new Map(s.pendingPermissions);
      const sessionPerms = pendingPermissions.get(sessionId);
      if (sessionPerms) {
        const updated = new Map(sessionPerms);
        updated.delete(requestId);
        pendingPermissions.set(sessionId, updated);
      }
      return { pendingPermissions };
    }),

  addTask: (sessionId, task) =>
    set((s) => {
      const sessionTasks = new Map(s.sessionTasks);
      const tasks = [...(sessionTasks.get(sessionId) || []), task];
      sessionTasks.set(sessionId, tasks);
      return { sessionTasks };
    }),

  setTasks: (sessionId, tasks) =>
    set((s) => {
      const sessionTasks = new Map(s.sessionTasks);
      sessionTasks.set(sessionId, tasks);
      return { sessionTasks };
    }),

  updateTask: (sessionId, taskId, updates) =>
    set((s) => {
      const sessionTasks = new Map(s.sessionTasks);
      const tasks = sessionTasks.get(sessionId);
      if (tasks) {
        sessionTasks.set(
          sessionId,
          tasks.map((t) => (t.id === taskId ? { ...t, ...updates } : t)),
        );
      }
      return { sessionTasks };
    }),

  addChangedFile: (sessionId, filePath) =>
    set((s) => {
      const changedFiles = new Map(s.changedFiles);
      const files = new Set(changedFiles.get(sessionId) || []);
      files.add(filePath);
      changedFiles.set(sessionId, files);
      return { changedFiles };
    }),

  clearChangedFiles: (sessionId) =>
    set((s) => {
      const changedFiles = new Map(s.changedFiles);
      changedFiles.delete(sessionId);
      return { changedFiles };
    }),

  setSessionName: (sessionId, name) =>
    set((s) => {
      const sessionNames = new Map(s.sessionNames);
      sessionNames.set(sessionId, name);
      return { sessionNames };
    }),

  markRecentlyRenamed: (sessionId) =>
    set((s) => {
      const recentlyRenamed = new Set(s.recentlyRenamed);
      recentlyRenamed.add(sessionId);
      return { recentlyRenamed };
    }),

  clearRecentlyRenamed: (sessionId) =>
    set((s) => {
      const recentlyRenamed = new Set(s.recentlyRenamed);
      recentlyRenamed.delete(sessionId);
      return { recentlyRenamed };
    }),

  setPreviousPermissionMode: (sessionId, mode) =>
    set((s) => {
      const previousPermissionMode = new Map(s.previousPermissionMode);
      previousPermissionMode.set(sessionId, mode);
      return { previousPermissionMode };
    }),

  setConnectionStatus: (sessionId, status) =>
    set((s) => {
      const connectionStatus = new Map(s.connectionStatus);
      connectionStatus.set(sessionId, status);
      return { connectionStatus };
    }),

  setCliConnected: (sessionId, connected) =>
    set((s) => {
      const cliConnected = new Map(s.cliConnected);
      cliConnected.set(sessionId, connected);
      return { cliConnected };
    }),

  setSessionStatus: (sessionId, status) =>
    set((s) => {
      const sessionStatus = new Map(s.sessionStatus);
      sessionStatus.set(sessionId, status);
      return { sessionStatus };
    }),

  setReconnecting: (sessionId, value) =>
    set((s) => {
      const reconnecting = new Map(s.reconnecting);
      reconnecting.set(sessionId, value);
      return { reconnecting };
    }),

  setReconnectGaveUp: (sessionId, value) =>
    set((s) => {
      const reconnectGaveUp = new Map(s.reconnectGaveUp);
      reconnectGaveUp.set(sessionId, value);
      return { reconnectGaveUp };
    }),

  setActiveTab: (tab) => set({ activeTab: tab }),
  setActiveView: (view) => set({ activeView: view }),
  setSplitViewActive: (active) => set({ splitViewActive: active }),
  toggleSplitView: () => set((s) => ({ splitViewActive: !s.splitViewActive })),
  setAgentMode: (mode) => set({ agentMode: mode }),
  setExecutionMode: (mode) => set({ executionMode: mode }),
  setPendingConfirmation: (confirm) => set({ pendingConfirmation: confirm }),

  setEditorOpenFile: (sessionId, filePath) =>
    set((s) => {
      const editorOpenFile = new Map(s.editorOpenFile);
      if (filePath) {
        editorOpenFile.set(sessionId, filePath);
      } else {
        editorOpenFile.delete(sessionId);
      }
      return { editorOpenFile };
    }),

  setEditorUrl: (sessionId, url) =>
    set((s) => {
      const editorUrl = new Map(s.editorUrl);
      editorUrl.set(sessionId, url);
      return { editorUrl };
    }),

  setEditorLoading: (sessionId, loading) =>
    set((s) => {
      const editorLoading = new Map(s.editorLoading);
      editorLoading.set(sessionId, loading);
      return { editorLoading };
    }),

  setAudioActive: (active) => {
    set({ audioActive: active });
  },
  setAudioMode: (mode) => {
    localStorage.setItem("cc-audio-mode", mode);
    set({ audioMode: mode });
  },
  setIsRecording: (recording) => set({ isRecording: recording }),
  setWebRTCStatus: (status) => set({ webrtcStatus: status }),
  setWebRTCTransport: (transport) => set({ webrtcTransport: transport }),
  setDesktopStreamActive: (active) => {
    set({ desktopStreamActive: active });
  },
  setDesktopRemoteStream: (stream) => set({ desktopRemoteStream: stream }),
  setDesktopStatus: (status) => set({ desktopStatus: status }),
  setDesktopStats: (stats) => set({ desktopStats: stats }),
  setGuardEnabled: (enabled) => {
    localStorage.setItem("cc-guard-enabled", String(enabled));
    set({ guardEnabled: enabled });
  },
  setVoiceMode: (mode) => set({ voiceMode: mode }),
  setActiveAudioInputLabel: (label) => set({ activeAudioInputLabel: label }),

  setPendingFocus: (target) => set({ pendingFocus: target }),
  setCommandPaletteOpen: (open) => set({ commandPaletteOpen: open }),
  toggleCommandPalette: () => set((s) => ({ commandPaletteOpen: !s.commandPaletteOpen })),
  setNodes: (nodes) => set({ nodes }),
  setActiveNode: (nodeId) => set({ activeNodeId: nodeId }),
  clearSessionState: () => {
    console.log(`[store] clearSessionState — resetting cliConnected/connectionStatus maps`);
    return set({
    sessions: new Map(),
    sdkSessions: [],
    messages: new Map(),
    archivedCount: new Map(),
    streaming: new Map(),
    streamingStartedAt: new Map(),
    streamingOutputTokens: new Map(),
    pendingPermissions: new Map(),
    connectionStatus: new Map(),
    cliConnected: new Map(),
    sessionStatus: new Map(),
    reconnecting: new Map(),
    reconnectGaveUp: new Map(),
    sessionTasks: new Map(),
    changedFiles: new Map(),
    sessionNames: new Map(),
    previousPermissionMode: new Map(),
  });},
  setPlaygroundActive: (active) => set({ playgroundActive: active }),
  setPlaygroundSessionId: (id) => set({ playgroundSessionId: id }),
  addPlaygroundSegment: (segment) => set((s) => ({
    playgroundSegments: [{ ...segment, timestamp: Date.now() }, ...s.playgroundSegments],
  })),
  clearPlaygroundSegments: () => set({ playgroundSegments: [] }),
  setPlaygroundLevel: (rmsDb) => set({ playgroundRmsDb: rmsDb }),
  setPlaygroundVadActive: (active) => set({ playgroundVadActive: active }),

  reset: () =>
    set({
      sessions: new Map(),
      sdkSessions: [],
      currentSessionId: null,
      messages: new Map(),
      streaming: new Map(),
      streamingStartedAt: new Map(),
      streamingOutputTokens: new Map(),
      pendingPermissions: new Map(),
      connectionStatus: new Map(),
      cliConnected: new Map(),
      sessionStatus: new Map(),
      previousPermissionMode: new Map(),
      sessionTasks: new Map(),
      changedFiles: new Map(),
      sessionNames: new Map(),
      recentlyRenamed: new Set(),
      activeTab: "chat" as const,
      activeView: "session" as const,
      splitViewActive: false,
      agentMode: "watch" as const,
      executionMode: "auto" as const,
      pendingConfirmation: null,
      editorOpenFile: new Map(),
      editorUrl: new Map(),
      editorLoading: new Map(),
      audioActive: false,
      audioMode: "off" as const,
      isRecording: false,
      webrtcStatus: null,
      webrtcTransport: null,
      desktopStreamActive: false,
      desktopRemoteStream: null,
      desktopStatus: "idle" as const,
      desktopStats: null,
      guardEnabled: true,
      voiceMode: null,
      activeAudioInputLabel: null,
      pendingFocus: null,
      commandPaletteOpen: false,
      secondScreenContent: null,
      playgroundActive: false,
      playgroundSessionId: null,
      playgroundSegments: [],
      playgroundRmsDb: -60,
      playgroundVadActive: false,
    }),
}));

