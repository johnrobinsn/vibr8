import { create } from "zustand";
import type { SessionState, PermissionRequest, ChatMessage, SdkSessionInfo, TaskItem } from "./types.js";

interface AppState {
  // Sessions
  sessions: Map<string, SessionState>;
  sdkSessions: SdkSessionInfo[];
  currentSessionId: string | null;

  // Messages per session
  messages: Map<string, ChatMessage[]>;

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
  sessionStatus: Map<string, "idle" | "running" | "compacting" | null>;

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
  editorOpenFile: Map<string, string>;
  editorUrl: Map<string, string>;
  editorLoading: Map<string, boolean>;

  // Reconnect state per session
  reconnecting: Map<string, boolean>;
  reconnectGaveUp: Map<string, boolean>;

  // WebRTC audio state per session
  audioMode: Map<string, "off" | "connecting" | "in_out" | "in_only">;
  isRecording: Map<string, boolean>;
  webrtcStatus: Map<string, string | null>;
  webrtcTransport: Map<string, "direct" | "relay" | null>;

  // Guard mode per session
  guardEnabled: Map<string, boolean>;

  // Focus management
  pendingFocus: "composer" | "terminal" | "home" | null;

  // Command palette
  commandPaletteOpen: boolean;

  // Actions
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
  setSessionStatus: (sessionId: string, status: "idle" | "running" | "compacting" | null) => void;
  setReconnecting: (sessionId: string, value: boolean) => void;
  setReconnectGaveUp: (sessionId: string, value: boolean) => void;

  // Editor actions
  setActiveTab: (tab: "chat" | "editor") => void;
  setEditorOpenFile: (sessionId: string, filePath: string | null) => void;
  setEditorUrl: (sessionId: string, url: string) => void;
  setEditorLoading: (sessionId: string, loading: boolean) => void;

  // WebRTC audio actions
  setAudioMode: (sessionId: string, mode: "off" | "connecting" | "in_out" | "in_only") => void;
  setIsRecording: (sessionId: string, recording: boolean) => void;
  setWebRTCStatus: (sessionId: string, status: string | null) => void;
  setWebRTCTransport: (sessionId: string, transport: "direct" | "relay" | null) => void;

  // Focus management actions
  setPendingFocus: (target: "composer" | "terminal" | "home" | null) => void;

  // Command palette actions
  setCommandPaletteOpen: (open: boolean) => void;
  toggleCommandPalette: () => void;

  // Guard mode actions
  setGuardEnabled: (sessionId: string, enabled: boolean) => void;

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
  darkMode: getInitialDarkMode(),
  notificationSound: getInitialNotificationSound(),
  sidebarOpen: typeof window !== "undefined" ? localStorage.getItem("cc-sidebar-open") !== "false" : true,
  taskPanelOpen: typeof window !== "undefined" ? localStorage.getItem("cc-task-panel-open") !== "false" && window.innerWidth >= 1024 : false,
  homeResetKey: 0,
  activeTab: "chat",
  editorOpenFile: new Map(),
  editorUrl: new Map(),
  editorLoading: new Map(),
  reconnecting: new Map(),
  reconnectGaveUp: new Map(),
  audioMode: new Map(),
  isRecording: new Map(),
  webrtcStatus: new Map(),
  webrtcTransport: new Map(),
  guardEnabled: new Map(),
  pendingFocus: null,
  commandPaletteOpen: false,

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
    if (id) {
      localStorage.setItem("cc-last-session", id);
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
      const audioMode = new Map(s.audioMode);
      audioMode.delete(sessionId);
      const isRecording = new Map(s.isRecording);
      isRecording.delete(sessionId);
      const webrtcStatus = new Map(s.webrtcStatus);
      webrtcStatus.delete(sessionId);
      const webrtcTransport = new Map(s.webrtcTransport);
      webrtcTransport.delete(sessionId);
      const guardEnabled = new Map(s.guardEnabled);
      guardEnabled.delete(sessionId);
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
        audioMode,
        isRecording,
        webrtcStatus,
        webrtcTransport,
        guardEnabled,
        sdkSessions: s.sdkSessions.filter((sdk) => sdk.sessionId !== sessionId),
        currentSessionId: s.currentSessionId === sessionId ? null : s.currentSessionId,
      };
    }),

  setSdkSessions: (sessions) => {
    set({ sdkSessions: sessions });
  },

  appendMessage: (sessionId, msg) =>
    set((s) => {
      const messages = new Map(s.messages);
      const list = [...(messages.get(sessionId) || []), msg];
      messages.set(sessionId, list);
      return { messages };
    }),

  setMessages: (sessionId, msgs) =>
    set((s) => {
      const messages = new Map(s.messages);
      messages.set(sessionId, msgs);
      return { messages };
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

  setAudioMode: (sessionId, mode) =>
    set((s) => {
      const audioMode = new Map(s.audioMode);
      audioMode.set(sessionId, mode);
      return { audioMode };
    }),

  setIsRecording: (sessionId, recording) =>
    set((s) => {
      const isRecording = new Map(s.isRecording);
      isRecording.set(sessionId, recording);
      return { isRecording };
    }),

  setWebRTCStatus: (sessionId, status) =>
    set((s) => {
      const webrtcStatus = new Map(s.webrtcStatus);
      if (status === null) {
        webrtcStatus.delete(sessionId);
      } else {
        webrtcStatus.set(sessionId, status);
      }
      return { webrtcStatus };
    }),

  setWebRTCTransport: (sessionId, transport) =>
    set((s) => {
      const webrtcTransport = new Map(s.webrtcTransport);
      if (transport === null) {
        webrtcTransport.delete(sessionId);
      } else {
        webrtcTransport.set(sessionId, transport);
      }
      return { webrtcTransport };
    }),

  setGuardEnabled: (sessionId, enabled) =>
    set((s) => {
      const guardEnabled = new Map(s.guardEnabled);
      guardEnabled.set(sessionId, enabled);
      return { guardEnabled };
    }),

  setPendingFocus: (target) => set({ pendingFocus: target }),
  setCommandPaletteOpen: (open) => set({ commandPaletteOpen: open }),
  toggleCommandPalette: () => set((s) => ({ commandPaletteOpen: !s.commandPaletteOpen })),

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
      editorOpenFile: new Map(),
      editorUrl: new Map(),
      editorLoading: new Map(),
      audioMode: new Map(),
      isRecording: new Map(),
      webrtcStatus: new Map(),
      webrtcTransport: new Map(),
      guardEnabled: new Map(),
      pendingFocus: null,
      commandPaletteOpen: false,
    }),
}));

