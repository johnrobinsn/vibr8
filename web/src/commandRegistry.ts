import { useStore } from "./store.js";
import { api } from "./api.js";
import { disconnectSession } from "./ws.js";
import { stopWebRTC } from "./webrtc.js";

export interface CommandContext {
  param?: string;
}

export type CommandResult = void | { needsInput: string };

export interface Command {
  id: string;
  label: string;
  description?: string;
  icon?: "session" | "ui";
  execute: (ctx: CommandContext) => CommandResult | Promise<CommandResult>;
}

export const commands: Command[] = [
  {
    id: "session.new",
    label: "New Session",
    icon: "session",
    execute: () => {
      const s = useStore.getState();
      if (s.currentSessionId) {
        const sdk = s.sdkSessions.find((x) => x.sessionId === s.currentSessionId);
        if (sdk?.backendType !== "terminal") {
          disconnectSession(s.currentSessionId);
        }
      }
      s.newSession();
      s.setPendingFocus("home");
    },
  },
  {
    id: "session.rename",
    label: "Rename Session",
    description: "Rename the current session",
    icon: "session",
    execute: (ctx) => {
      const s = useStore.getState();
      if (!s.currentSessionId) return;
      if (!ctx.param) {
        return { needsInput: "Enter new name" };
      }
      const name = ctx.param.trim();
      if (!name) return;
      s.setSessionName(s.currentSessionId, name);
      api.renameSession(s.currentSessionId, name).catch(() => {});
    },
  },
  {
    id: "session.archive",
    label: "Archive Session",
    description: "Archive the current session",
    icon: "session",
    execute: async () => {
      const s = useStore.getState();
      if (!s.currentSessionId) return;
      const sid = s.currentSessionId;
      try {
        stopWebRTC(sid);
        disconnectSession(sid);
        await api.archiveSession(sid);
      } catch { /* best-effort */ }
      s.newSession();
      try {
        const list = await api.listSessions();
        s.setSdkSessions(list);
      } catch { /* best-effort */ }
    },
  },
  {
    id: "ui.sidebar",
    label: "Toggle Sidebar",
    description: "Ctrl+Alt+S",
    icon: "ui",
    execute: () => {
      const s = useStore.getState();
      s.setSidebarOpen(!s.sidebarOpen);
    },
  },
  {
    id: "ui.darkMode",
    label: "Toggle Dark Mode",
    icon: "ui",
    execute: () => {
      useStore.getState().toggleDarkMode();
    },
  },
  {
    id: "ui.sound",
    label: "Toggle Notification Sound",
    icon: "ui",
    execute: () => {
      useStore.getState().toggleNotificationSound();
    },
  },
  {
    id: "ui.taskPanel",
    label: "Toggle Task Panel",
    icon: "ui",
    execute: () => {
      const s = useStore.getState();
      s.setTaskPanelOpen(!s.taskPanelOpen);
    },
  },
  {
    id: "secondscreen.pair",
    label: "Pair Second Screen",
    description: "Enter a pairing code from a second screen",
    icon: "ui",
    execute: async (ctx) => {
      if (!ctx.param) {
        return { needsInput: "Enter pairing code from second screen" };
      }
      const code = ctx.param.trim();
      if (!code) return;
      try {
        await api.secondScreenPair(code);
      } catch (err) {
        console.error("[second-screen] Pairing failed:", err);
      }
    },
  },
];
