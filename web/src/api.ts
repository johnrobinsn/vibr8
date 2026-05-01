import type { SdkSessionInfo, NodeInfo, AndroidDeviceInfo, DiscoveredDevice, MdnsDevice } from "./types.js";

const BASE = "/api";

// Device token for second screen authentication (set after pairing)
let _deviceToken: string | null = null;

export function setDeviceToken(token: string | null): void {
  _deviceToken = token;
  if (token) {
    localStorage.setItem("vibr8_device_token", token);
  } else {
    localStorage.removeItem("vibr8_device_token");
  }
}

export function getDeviceToken(): string | null {
  if (_deviceToken) return _deviceToken;
  const stored = localStorage.getItem("vibr8_device_token");
  if (stored) _deviceToken = stored;
  return _deviceToken;
}

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...extra };
  const token = getDeviceToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

function checkAuth(res: Response): void {
  if (res.status === 401) {
    // Don't reload if we're a second screen device — the token may have been revoked
    if (getDeviceToken()) {
      throw new Error("Device token rejected");
    }
    window.location.reload();
    throw new Error("Session expired");
  }
}

async function post<T = unknown>(path: string, body?: object): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: body ? JSON.stringify(body) : undefined,
  });
  checkAuth(res);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || res.statusText);
  }
  return res.json();
}

async function get<T = unknown>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { headers: authHeaders() });
  checkAuth(res);
  if (!res.ok) throw new Error(res.statusText);
  return res.json();
}

async function put<T = unknown>(path: string, body?: object): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "PUT",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || res.statusText);
  }
  return res.json();
}

async function patch<T = unknown>(path: string, body?: object): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "PATCH",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || res.statusText);
  }
  return res.json();
}

async function del<T = unknown>(path: string, body?: object): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "DELETE",
    headers: authHeaders(body ? { "Content-Type": "application/json" } : undefined),
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || res.statusText);
  }
  return res.json();
}

async function uploadFile(sessionId: string, file: File): Promise<{ path: string }> {
  const form = new FormData();
  form.append("file", file);
  const headers: Record<string, string> = {};
  const token = getDeviceToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const res = await fetch(`${BASE}/sessions/${sessionId}/upload`, {
    method: "POST",
    headers,
    body: form,
  });
  checkAuth(res);
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || res.statusText);
  }
  return res.json();
}

export interface CreateSessionOpts {
  model?: string;
  permissionMode?: string;
  cwd?: string;
  claudeBinary?: string;
  codexBinary?: string;
  allowedTools?: string[];
  envSlug?: string;
  branch?: string;
  createBranch?: boolean;
  useWorktree?: boolean;
  backend?: "claude" | "codex" | "opencode" | "terminal" | "computer-use";
  nodeId?: string;
  agentType?: string;
  agentConfig?: Record<string, unknown>;
}

export interface BackendInfo {
  id: string;
  name: string;
  available: boolean;
}

export interface BackendModelInfo {
  value: string;
  label: string;
  description: string;
}

export interface GitRepoInfo {
  repoRoot: string;
  repoName: string;
  currentBranch: string;
  defaultBranch: string;
  isWorktree: boolean;
}

export interface GitBranchInfo {
  name: string;
  isCurrent: boolean;
  isRemote: boolean;
  worktreePath: string | null;
  ahead: number;
  behind: number;
}

export interface GitWorktreeInfo {
  path: string;
  branch: string;
  head: string;
  isMainWorktree: boolean;
  isDirty: boolean;
}

export interface WorktreeCreateResult {
  worktreePath: string;
  branch: string;
  isNew: boolean;
}

export interface Vibr8Env {
  name: string;
  slug: string;
  variables: Record<string, string>;
  createdAt: number;
  updatedAt: number;
}

export interface DirEntry {
  name: string;
  path: string;
}

export interface DirListResult {
  path: string;
  dirs: DirEntry[];
  home: string;
  error?: string;
}

export interface TreeNode {
  name: string;
  path: string;
  type: "file" | "directory";
  children?: TreeNode[];
}

export interface ClientMetadata {
  clientId: string;
  name?: string;
  description?: string;
  role?: string;
  deviceInfo?: Record<string, unknown>;
  fingerprint?: string;
  online?: boolean;
  sessionId?: string;
  wsRole?: string;
  lastSeen?: number;
  createdAt?: number;
}

export interface UsageLimits {
  five_hour: { utilization: number; resets_at: string | null } | null;
  seven_day: { utilization: number; resets_at: string | null } | null;
  extra_usage: {
    is_enabled: boolean;
    monthly_limit: number;
    used_credits: number;
    utilization: number | null;
  } | null;
}

export interface VoiceProfile {
  id: string | null;
  name: string;
  user: string;
  micGain: number;
  vadThresholdDb: number;
  sileroVadThreshold: number;
  eouThreshold: number;
  eouMaxRetries: number;
  minSegmentDuration: number;
  promptTimeoutMs: number;
  isActive: boolean;
  createdAt?: number;
  updatedAt?: number;
}

export interface SpeakerFingerprint {
  id: string;
  name: string;
  user: string;
  createdAt: number;
  embeddingCount: number;
  embeddingLabels: string[];
  embeddingIds: string[];
}

export interface ActiveFingerprint {
  speakerName: string | null;
  threshold: number;
}

export interface VoiceSegment {
  id: string;
  sessionId: string;
  transcript: string;
  timeBegin: number;
  timeEnd: number;
  recordingId: string | null;
  profileId: string | null;
  segParamsId?: string | null;
  eouProb?: number | null;
  createdAt: number;
}

export interface VoiceSegParams {
  id: string;
  profileId: string | null;
  profileName: string | null;
  params: {
    mic_gain: number;
    vad_threshold_db: number;
    silero_vad_threshold: number;
    eou_threshold: number;
    eou_max_retries: number;
    min_segment_duration: number;
  };
  createdAt: number;
}

export interface VoiceRecording {
  id: string;
  sessionId: string;
  duration: number;
  startedAt: number;
  endedAt: number | null;
}

export const api = {
  createSession: (opts?: CreateSessionOpts) =>
    post<{ sessionId: string; state: string; cwd: string }>(
      "/sessions/create",
      opts,
    ),

  listSessions: (nodeId?: string) =>
    get<SdkSessionInfo[]>(nodeId ? `/sessions?nodeId=${encodeURIComponent(nodeId)}` : "/sessions"),

  listAgents: () =>
    get<{ id: string; name: string; resourceType: string; configSchema: Record<string, unknown>; defaultConfig: Record<string, unknown> }[]>("/agents"),

  killSession: (sessionId: string) =>
    post(`/sessions/${encodeURIComponent(sessionId)}/kill`),

  deleteSession: (sessionId: string) =>
    del(`/sessions/${encodeURIComponent(sessionId)}`),

  relaunchSession: (sessionId: string) =>
    post(`/sessions/${encodeURIComponent(sessionId)}/relaunch`),

  archiveSession: (sessionId: string, opts?: { force?: boolean }) =>
    post(`/sessions/${encodeURIComponent(sessionId)}/archive`, opts),

  unarchiveSession: (sessionId: string) =>
    post(`/sessions/${encodeURIComponent(sessionId)}/unarchive`),

  renameSession: (sessionId: string, name: string) =>
    patch<{ ok: boolean; name: string }>(
      `/sessions/${encodeURIComponent(sessionId)}/name`,
      { name },
    ),

  listDirs: (path?: string) =>
    get<DirListResult>(
      `/fs/list${path ? `?path=${encodeURIComponent(path)}` : ""}`,
    ),

  getHome: () => get<{ home: string; cwd: string }>("/fs/home"),

  // Environments
  listEnvs: () => get<Vibr8Env[]>("/envs"),
  getEnv: (slug: string) =>
    get<Vibr8Env>(`/envs/${encodeURIComponent(slug)}`),
  createEnv: (name: string, variables: Record<string, string>) =>
    post<Vibr8Env>("/envs", { name, variables }),
  updateEnv: (
    slug: string,
    data: { name?: string; variables?: Record<string, string> },
  ) => put<Vibr8Env>(`/envs/${encodeURIComponent(slug)}`, data),
  deleteEnv: (slug: string) => del(`/envs/${encodeURIComponent(slug)}`),

  // Git operations
  getRepoInfo: (path: string) =>
    get<GitRepoInfo>(`/git/repo-info?path=${encodeURIComponent(path)}`),
  listBranches: (repoRoot: string) =>
    get<GitBranchInfo[]>(
      `/git/branches?repoRoot=${encodeURIComponent(repoRoot)}`,
    ),
  listWorktrees: (repoRoot: string) =>
    get<GitWorktreeInfo[]>(
      `/git/worktrees?repoRoot=${encodeURIComponent(repoRoot)}`,
    ),
  createWorktree: (
    repoRoot: string,
    branch: string,
    opts?: { baseBranch?: string; createBranch?: boolean },
  ) =>
    post<WorktreeCreateResult>("/git/worktree", { repoRoot, branch, ...opts }),
  removeWorktree: (repoRoot: string, worktreePath: string, force?: boolean) =>
    del<{ removed: boolean; reason?: string }>("/git/worktree", {
      repoRoot,
      worktreePath,
      force,
    }),
  gitFetch: (repoRoot: string) =>
    post<{ success: boolean; output: string }>("/git/fetch", { repoRoot }),
  gitPull: (cwd: string) =>
    post<{
      success: boolean;
      output: string;
      git_ahead: number;
      git_behind: number;
    }>("/git/pull", { cwd }),

  // Backends
  getBackends: () => get<BackendInfo[]>("/backends"),
  getBackendModels: (backendId: string) =>
    get<BackendModelInfo[]>(`/backends/${encodeURIComponent(backendId)}/models`),

  // Editor
  startEditor: (sessionId: string) =>
    post<{ url: string }>(
      `/sessions/${encodeURIComponent(sessionId)}/editor/start`,
    ),

  // Editor filesystem
  getFileTree: (path: string) =>
    get<{ path: string; tree: TreeNode[] }>(
      `/fs/tree?path=${encodeURIComponent(path)}`,
    ),
  readFile: (path: string) =>
    get<{ path: string; content: string }>(
      `/fs/read?path=${encodeURIComponent(path)}`,
    ),
  writeFile: (path: string, content: string) =>
    put<{ ok: boolean; path: string }>("/fs/write", { path, content }),
  mkdir: (path: string) =>
    post<{ ok: boolean; path: string }>("/fs/mkdir", { path }),
  uploadToSession: (sessionId: string, file: File) =>
    uploadFile(sessionId, file),

  deleteFile: (path: string) =>
    post<{ ok: boolean }>("/fs/delete", { path }),
  rename: (oldPath: string, newPath: string) =>
    post<{ ok: boolean }>("/fs/rename", { oldPath, newPath }),
  getFileDiff: (path: string) =>
    get<{ path: string; diff: string }>(
      `/fs/diff?path=${encodeURIComponent(path)}`,
    ),

  // Usage limits
  getUsageLimits: () => get<UsageLimits>("/usage-limits"),

  // Guard mode (client-scoped)
  setGuardByClient: (clientId: string, enabled: boolean) =>
    post<{ ok: boolean; enabled: boolean }>(
      `/clients/${encodeURIComponent(clientId)}/guard`,
      { enabled },
    ),

  // TTS mute (client-scoped)
  setTtsMutedByClient: (clientId: string, muted: boolean) =>
    post<{ ok: boolean; muted: boolean }>(
      `/clients/${encodeURIComponent(clientId)}/tts-mute`,
      { muted },
    ),

  // Ring0 meta-agent
  getRing0Status: () =>
    get<{ enabled: boolean; eventsMuted: boolean; sessionId: string | null }>("/ring0/status"),

  toggleRing0: (enabled: boolean) =>
    post<{ ok: boolean; enabled: boolean; sessionId: string | null }>(
      "/ring0/toggle",
      { enabled },
    ),

  muteRing0Events: (muted: boolean) =>
    post<{ ok: boolean; eventsMuted: boolean }>("/ring0/mute-events", { muted }),

  // WebRTC signaling
  getIceServers: () =>
    get<{ iceServers: RTCIceServer[] }>("/webrtc/ice-servers"),

  webrtcOffer: (clientId: string, offer: { sdp: string; type: string }, opts?: { playground?: boolean; profileId?: string; desktop?: boolean; desktopRole?: string; nodeId?: string }) =>
    post<{ sdp: string; type: string }>("/webrtc/offer", {
      clientId,
      sdp: offer.sdp,
      type: offer.type,
      ...(opts?.playground ? { playground: true } : {}),
      ...(opts?.profileId ? { profileId: opts.profileId } : {}),
      ...(opts?.desktop ? { desktop: true } : {}),
      ...(opts?.desktopRole ? { desktopRole: opts.desktopRole } : {}),
      ...(opts?.nodeId ? { nodeId: opts.nodeId } : {}),
    }),

  // Voice Profiles
  listVoiceProfiles: () => get<VoiceProfile[]>("/voice/profiles"),
  createVoiceProfile: (data: Partial<VoiceProfile>) => post<VoiceProfile>("/voice/profiles", data),
  updateVoiceProfile: (id: string, data: Partial<VoiceProfile>) => put<VoiceProfile>(`/voice/profiles/${encodeURIComponent(id)}`, data),
  deleteVoiceProfile: (id: string) => del(`/voice/profiles/${encodeURIComponent(id)}`),
  activateVoiceProfile: (id: string) => post<VoiceProfile>(`/voice/profiles/${encodeURIComponent(id)}/activate`),
  deactivateVoiceProfiles: () => post("/voice/profiles/deactivate"),
  getActiveVoiceProfile: () => get<VoiceProfile>("/voice/profiles/active"),

  // Speaker Fingerprints
  listFingerprints: () => get<SpeakerFingerprint[]>("/voice/fingerprints"),
  createFingerprint: (data: { name: string; embedding: number[]; label?: string; audio?: string }) => post<SpeakerFingerprint>("/voice/fingerprints", data),
  deleteFingerprint: (id: string) => del(`/voice/fingerprints/${encodeURIComponent(id)}`),
  getActiveFingerprint: () => get<ActiveFingerprint>("/voice/fingerprints/active"),
  setActiveFingerprint: (data: { speakerName: string | null; threshold?: number }) => put<ActiveFingerprint>("/voice/fingerprints/active", data),
  addEmbedding: (profileId: string, data: { embedding: number[]; label?: string; audio?: string }) => post<SpeakerFingerprint>(`/voice/fingerprints/${encodeURIComponent(profileId)}/embeddings`, data),
  removeEmbedding: (profileId: string, embId: string) => del(`/voice/fingerprints/${encodeURIComponent(profileId)}/embeddings/${encodeURIComponent(embId)}`),
  refreshSpeakerGate: () => post("/voice/fingerprints/refresh"),

  // Voice Recordings
  listVoiceRecordings: () => get<VoiceRecording[]>("/voice/recordings"),

  // Voice Seg Params
  getSegParams: (id: string) => get<VoiceSegParams>(`/voice/seg-params/${encodeURIComponent(id)}`),

  // Voice Logs
  listVoiceLogs: (opts?: { q?: string; offset?: number; limit?: number }) => {
    const params = new URLSearchParams();
    if (opts?.q) params.set("q", opts.q);
    if (opts?.offset) params.set("offset", String(opts.offset));
    if (opts?.limit) params.set("limit", String(opts.limit));
    const qs = params.toString();
    return get<VoiceSegment[]>(`/voice/logs${qs ? `?${qs}` : ""}`);
  },
  deleteVoiceLog: (id: string) => del(`/voice/logs/${encodeURIComponent(id)}`),
  clearVoiceLogs: () => del("/voice/logs"),

  // Second Screen
  secondScreenPairCode: (clientId: string) => post<{ code: string }>("/second-screen/pair-code", { clientId }),
  secondScreenPair: (code: string) => post<{ ok: boolean; secondScreenClientId: string }>("/second-screen/pair", { code }),
  secondScreenStatus: (clientId: string) => get<{ paired: boolean; role?: string; pairedUser?: string; pairedAt?: number; screens?: Array<{ clientId: string; pairedUser: string; pairedAt: number }> }>(`/second-screen/status?clientId=${encodeURIComponent(clientId)}`),
  secondScreenUnpair: (clientId: string) => post<{ ok: boolean }>("/second-screen/unpair", { clientId }),
  secondScreenToggle: (clientId: string, enabled: boolean) => post<{ ok: boolean; enabled: boolean }>("/second-screen/toggle", { clientId, enabled }),
  secondScreenList: () => get<Array<{ clientId: string; pairedUser: string; pairedAt: number; enabled: boolean; online: boolean }>>("/second-screen/list"),

  // Client metadata
  getClients: () => get<ClientMetadata[]>("/clients"),
  getClientMetadata: (clientId: string) =>
    get<ClientMetadata>(`/clients/${encodeURIComponent(clientId)}`),
  updateClientMetadata: (clientId: string, data: { name?: string; description?: string; role?: string }) =>
    put<ClientMetadata>(`/clients/${encodeURIComponent(clientId)}`, data),
  reportDeviceInfo: (clientId: string, info: Record<string, unknown>) =>
    post<ClientMetadata>(`/clients/${encodeURIComponent(clientId)}/device-info`, info),

  // Pen control
  setPen: (sessionId: string, controlledBy: "ring0" | "user") =>
    post(`/sessions/${encodeURIComponent(sessionId)}/pen`, { controlledBy }),

  // Nodes
  listNodes: () => get<NodeInfo[]>("/nodes"),
  activateNode: (nodeId: string) =>
    post<{ ok: boolean; nodeId: string; name: string }>(
      `/nodes/${encodeURIComponent(nodeId)}/activate`,
    ),
  getActiveNode: () =>
    get<{ nodeId: string; name: string; status: string }>("/nodes/active"),

  // Node API keys
  generateNodeKey: (name: string) =>
    post<{ apiKey: string; id: string; name: string; keyPrefix: string; createdAt: number; lastUsedAt: number }>("/nodes/generate-key", { name }),
  listNodeKeys: () =>
    get<Array<{ id: string; name: string; keyPrefix: string; createdAt: number; lastUsedAt: number }>>("/nodes/keys"),
  revokeNodeKey: (keyId: string) =>
    del(`/nodes/keys/${encodeURIComponent(keyId)}`),

  // Device tokens
  createDeviceToken: (name: string) =>
    post<{ token: string; tokenId: string; name: string; createdAt: number }>("/auth/device-token", { name }),
  listDeviceTokens: () =>
    get<{ tokens: Array<{ id: string; name: string; createdAt: number; lastUsedAt: number | null }> }>("/auth/device-tokens"),
  revokeDeviceToken: (tokenId: string) =>
    del(`/auth/device-tokens/${encodeURIComponent(tokenId)}`),
  confirmPairing: (code: string, name: string) =>
    post<{ ok: boolean; type: string }>("/pairing/confirm", { code, name }),

  // Android Devices
  listAndroidDevices: () => get<AndroidDeviceInfo[]>("/android/devices"),

  registerAndroidDevice: (data: { name: string; connectionMode: string; deviceId: string; ip?: string; port?: number }) =>
    post<AndroidDeviceInfo>("/android/devices", data),

  updateAndroidDevice: (nodeId: string, data: { name?: string; ip?: string; port?: number; connectionMode?: string; deviceId?: string }) =>
    put<AndroidDeviceInfo>(`/android/devices/${encodeURIComponent(nodeId)}`, data),

  deleteAndroidDevice: (nodeId: string) =>
    del(`/android/devices/${encodeURIComponent(nodeId)}`),

  discoverAndroidDevices: () =>
    get<{ usb: DiscoveredDevice[]; mdns: MdnsDevice[] }>("/android/discover"),

  connectAndroidDevice: (nodeId: string) =>
    post<{ ok: boolean; status: string }>("/android/connect", { nodeId }),

  disconnectAndroidDevice: (nodeId: string) =>
    post<{ ok: boolean }>(`/android/disconnect/${encodeURIComponent(nodeId)}`),

  androidDeviceStatus: (nodeId: string) =>
    get<{ online: boolean; node: AndroidDeviceInfo }>(`/android/devices/${encodeURIComponent(nodeId)}/status`),

  androidWebrtcOffer: (nodeId: string, offer: { sdp: string; type: string }) =>
    post<{ sdp: string; type: string }>(`/android/devices/${encodeURIComponent(nodeId)}/webrtc/offer`, {
      sdp: offer.sdp,
      type: offer.type,
    }),

  listAllNodes: () => get<Array<NodeInfo & { nodeType?: string; connectionMode?: string; deviceId?: string; capabilities?: Record<string, unknown> }>>("/nodes/all"),

  // Admin
  restartServer: () => post("/admin/restart"),
};
