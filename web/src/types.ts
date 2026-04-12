import type {
  SessionState,
  PermissionRequest,
  ContentBlock,
  BrowserIncomingMessage,
  BrowserOutgoingMessage,
  BackendType,
} from "../server/session-types.js";

export type { SessionState, PermissionRequest, ContentBlock, BrowserIncomingMessage, BrowserOutgoingMessage, BackendType };

export interface EventMeta {
  eventType: string;
  summary?: string;
  ui: "visible" | "collapsed" | "hidden";
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  contentBlocks?: ContentBlock[];
  images?: { media_type: string; data: string }[];
  timestamp: number;
  parentToolUseId?: string | null;
  isStreaming?: boolean;
  model?: string;
  stopReason?: string | null;
  eventMeta?: EventMeta;
}

export interface TaskItem {
  id: string;
  subject: string;
  description: string;
  activeForm?: string;
  status: "pending" | "in_progress" | "completed";
  owner?: string;
  blockedBy?: string[];
}

export interface SdkSessionInfo {
  sessionId: string;
  pid?: number;
  state: "starting" | "connected" | "running" | "exited";
  exitCode?: number | null;
  model?: string;
  permissionMode?: string;
  cwd: string;
  createdAt: number;
  archived?: boolean;
  isWorktree?: boolean;
  repoRoot?: string;
  branch?: string;
  actualBranch?: string;
  name?: string;
  backendType?: BackendType;
  nodeId?: string;
  lastPromptedAt?: number;
  isRing0?: boolean;
  agentState?: "idle" | "running" | "waiting_for_permission" | "compacting";
}

export interface NodeInfo {
  id: string;
  name: string;
  status: "online" | "offline";
  platform: string;
  hostname: string;
  sessionCount: number;
  ring0Enabled: boolean;
}

export interface AndroidDeviceInfo {
  id: string;
  name: string;
  nodeType: "android";
  connectionMode: "usb" | "ip" | "mdns";
  deviceId: string;
  status: "online" | "offline" | "unauthorized";
  ip: string | null;
  port: number | null;
  capabilities: {
    canRunSessions: boolean;
    hasDisplay: boolean;
    nodeType: string;
    model?: string;
    manufacturer?: string;
    androidVersion?: string;
    screenWidth?: number;
    screenHeight?: number;
  };
  canRunSessions: boolean;
  hasDisplay: boolean;
  lastSeen: number;
}

export interface DiscoveredDevice {
  serial: string;
  model: string;
  status: string;
  transportId: string;
}

export interface MdnsDevice {
  name: string;
  ip: string;
  port: number;
}
