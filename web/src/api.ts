import type { SdkSessionInfo } from "./types.js";

const BASE = "/api";

function checkAuth(res: Response): void {
  if (res.status === 401) {
    window.location.reload();
    throw new Error("Session expired");
  }
}

async function post<T = unknown>(path: string, body?: object): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
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
  const res = await fetch(`${BASE}${path}`);
  checkAuth(res);
  if (!res.ok) throw new Error(res.statusText);
  return res.json();
}

async function put<T = unknown>(path: string, body?: object): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
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
    headers: { "Content-Type": "application/json" },
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
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
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
  backend?: "claude" | "codex" | "terminal";
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
  isActive: boolean;
  createdAt?: number;
  updatedAt?: number;
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

  listSessions: () => get<SdkSessionInfo[]>("/sessions"),

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
  getFileDiff: (path: string) =>
    get<{ path: string; diff: string }>(
      `/fs/diff?path=${encodeURIComponent(path)}`,
    ),

  // Usage limits
  getUsageLimits: () => get<UsageLimits>("/usage-limits"),

  // Guard mode
  setGuard: (sessionId: string, enabled: boolean) =>
    post<{ ok: boolean; enabled: boolean }>(
      `/sessions/${encodeURIComponent(sessionId)}/guard`,
      { enabled },
    ),

  // TTS mute (audio in-only mode)
  setTtsMuted: (sessionId: string, muted: boolean) =>
    post<{ ok: boolean; muted: boolean }>(
      `/sessions/${encodeURIComponent(sessionId)}/tts-mute`,
      { muted },
    ),

  // Ring0 meta-agent
  getRing0Status: () =>
    get<{ enabled: boolean; sessionId: string | null }>("/ring0/status"),

  toggleRing0: (enabled: boolean) =>
    post<{ ok: boolean; enabled: boolean; sessionId: string | null }>(
      "/ring0/toggle",
      { enabled },
    ),

  // WebRTC signaling
  getIceServers: () =>
    get<{ iceServers: RTCIceServer[] }>("/webrtc/ice-servers"),

  webrtcOffer: (sessionId: string, offer: { sdp: string; type: string }, clientId?: string, opts?: { playground?: boolean; profileId?: string }) =>
    post<{ sdp: string; type: string }>("/webrtc/offer", {
      sessionId,
      sdp: offer.sdp,
      type: offer.type,
      ...(clientId ? { clientId } : {}),
      ...(opts?.playground ? { playground: true } : {}),
      ...(opts?.profileId ? { profileId: opts.profileId } : {}),
    }),

  // Voice Profiles
  listVoiceProfiles: () => get<VoiceProfile[]>("/voice/profiles"),
  createVoiceProfile: (data: Partial<VoiceProfile>) => post<VoiceProfile>("/voice/profiles", data),
  updateVoiceProfile: (id: string, data: Partial<VoiceProfile>) => put<VoiceProfile>(`/voice/profiles/${encodeURIComponent(id)}`, data),
  deleteVoiceProfile: (id: string) => del(`/voice/profiles/${encodeURIComponent(id)}`),
  activateVoiceProfile: (id: string) => post<VoiceProfile>(`/voice/profiles/${encodeURIComponent(id)}/activate`),
  deactivateVoiceProfiles: () => post("/voice/profiles/deactivate"),
  getActiveVoiceProfile: () => get<VoiceProfile>("/voice/profiles/active"),

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
  secondScreenPair: (code: string, clientId: string) => post<{ ok: boolean; secondScreenClientId: string }>("/second-screen/pair", { code, clientId }),
  secondScreenStatus: (clientId: string) => get<{ paired: boolean; role?: string; pairedClientId?: string; pairedAt?: number; screens?: Array<{ clientId: string; pairedClientId: string; pairedAt: number }> }>(`/second-screen/status?clientId=${encodeURIComponent(clientId)}`),
  secondScreenUnpair: (clientId: string) => post<{ ok: boolean }>("/second-screen/unpair", { clientId }),
  secondScreenList: () => get<Array<{ clientId: string; pairedClientId: string; pairedAt: number; online: boolean }>>("/second-screen/list"),
};
