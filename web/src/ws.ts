import { useStore } from "./store.js";
import { stopWebRTC, getRemoteAudio, queryActiveInputDevice } from "./webrtc.js";
import type { BrowserIncomingMessage, BrowserOutgoingMessage, ContentBlock, ChatMessage, TaskItem } from "./types.js";
import { playNotificationSound } from "./utils/notification-sound.js";

// Store on window to survive Vite HMR module reloads (prevents duplicate WebSockets)
const _w = window as unknown as Record<string, unknown>;
const sockets = (_w.__v8_sockets ??= new Map<string, WebSocket>()) as Map<string, WebSocket>;
const reconnectTimers = (_w.__v8_reconnectTimers ??= new Map()) as Map<string, ReturnType<typeof setTimeout>>;
const reconnectStartTimes = (_w.__v8_reconnectStartTimes ??= new Map()) as Map<string, number>;
const RECONNECT_TIMEOUT_MS = 60_000;
const RECONNECT_INTERVAL_MS = 2_000;
const taskCounters = (_w.__v8_taskCounters ??= new Map()) as Map<string, number>;
/** Track processed tool_use IDs to prevent duplicate task creation */
const processedToolUseIds = (_w.__v8_processedToolUseIds ??= new Map()) as Map<string, Set<string>>;

// Expose clientId globally so Android native layer can read it via evaluateJavascript
_w.__v8_clientId = useStore.getState().clientId;

function getProcessedSet(sessionId: string): Set<string> {
  let set = processedToolUseIds.get(sessionId);
  if (!set) {
    set = new Set();
    processedToolUseIds.set(sessionId, set);
  }
  return set;
}

function extractTasksFromBlocks(sessionId: string, blocks: ContentBlock[]) {
  const store = useStore.getState();
  const processed = getProcessedSet(sessionId);

  for (const block of blocks) {
    if (block.type !== "tool_use") continue;
    const { name, input, id: toolUseId } = block;

    // Deduplicate by tool_use_id
    if (toolUseId) {
      if (processed.has(toolUseId)) continue;
      processed.add(toolUseId);
    }

    // TodoWrite: full replacement — { todos: [{ content, status, activeForm }] }
    if (name === "TodoWrite") {
      const todos = input.todos as { content?: string; status?: string; activeForm?: string }[] | undefined;
      if (Array.isArray(todos)) {
        const tasks: TaskItem[] = todos.map((t, i) => ({
          id: String(i + 1),
          subject: t.content || "Task",
          description: "",
          activeForm: t.activeForm,
          status: (t.status as TaskItem["status"]) || "pending",
        }));
        store.setTasks(sessionId, tasks);
        taskCounters.set(sessionId, tasks.length);
      }
      continue;
    }

    // TaskCreate: incremental add — { subject, description, activeForm }
    if (name === "TaskCreate") {
      const count = (taskCounters.get(sessionId) || 0) + 1;
      taskCounters.set(sessionId, count);
      const task = {
        id: String(count),
        subject: (input.subject as string) || "Task",
        description: (input.description as string) || "",
        activeForm: input.activeForm as string | undefined,
        status: "pending" as const,
      };
      store.addTask(sessionId, task);
      continue;
    }

    // TaskUpdate: incremental update — { taskId, status, owner, activeForm, addBlockedBy }
    if (name === "TaskUpdate") {
      const taskId = input.taskId as string;
      if (taskId) {
        const updates: Partial<TaskItem> = {};
        if (input.status) updates.status = input.status as TaskItem["status"];
        if (input.owner) updates.owner = input.owner as string;
        if (input.activeForm !== undefined) updates.activeForm = input.activeForm as string;
        if (input.addBlockedBy) updates.blockedBy = input.addBlockedBy as string[];
        store.updateTask(sessionId, taskId, updates);
      }
    }
  }
}

function extractChangedFilesFromBlocks(sessionId: string, blocks: ContentBlock[]) {
  const store = useStore.getState();
  for (const block of blocks) {
    if (block.type !== "tool_use") continue;
    const { name, input } = block;
    if ((name === "Edit" || name === "Write") && typeof input.file_path === "string") {
      store.addChangedFile(sessionId, input.file_path);
    }
  }
}

let idCounter = 0;
function nextId(): string {
  return `msg-${Date.now()}-${++idCounter}`;
}

export function getWsUrl(sessionId: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const { clientId, clientRole } = useStore.getState();
  let url = `${proto}//${location.host}/ws/browser/${sessionId}?clientId=${encodeURIComponent(clientId)}`;
  if (clientRole !== "primary") url += `&role=${encodeURIComponent(clientRole)}`;
  return url;
}

function extractTextFromBlocks(blocks: ContentBlock[]): string {
  return blocks
    .map((b) => {
      if (b.type === "text") return b.text;
      if (b.type === "thinking") return b.thinking;
      return "";
    })
    .filter(Boolean)
    .join("\n");
}

export function handleMessage(sessionId: string, event: MessageEvent, sourceWs?: WebSocket) {
  const store = useStore.getState();
  let data: BrowserIncomingMessage;
  try {
    data = JSON.parse(event.data);
  } catch {
    return;
  }

  switch (data.type) {
    case "session_init": {
      console.log(`[ws] session_init → cliConnected=true session=${sessionId.slice(0,8)}`);
      store.addSession(data.session);
      store.setCliConnected(sessionId, true);
      // Don't overwrite optimistic "running" status on init
      if (store.sessionStatus.get(sessionId) !== "running") {
        store.setSessionStatus(sessionId, "idle");
      }
      if (!store.sessionNames.has(sessionId)) {
        // Prefer server-assigned name from SDK session list
        const sdkSession = store.sdkSessions.find((s) => s.sessionId === sessionId);
        if (sdkSession?.name) {
          store.setSessionName(sessionId, sdkSession.name);
        }
      }
      break;
    }

    case "session_update": {
      store.updateSession(sessionId, data.session);
      break;
    }

    case "assistant": {
      const msg = data.message;
      const textContent = extractTextFromBlocks(msg.content);
      const chatMsg: ChatMessage = {
        id: msg.id,
        role: "assistant",
        content: textContent,
        contentBlocks: msg.content,
        timestamp: Date.now(),
        parentToolUseId: data.parent_tool_use_id,
        model: msg.model,
        stopReason: msg.stop_reason,
      };
      const existingMsgs = store.messages.get(sessionId) || [];
      const isDup = existingMsgs.some((m) => m.id === msg.id);
      console.log(`[ws] assistant msg id=${msg.id} session=${sessionId.slice(0,8)} dup=${isDup} existing=${existingMsgs.length} sockets=${sockets.size}`);
      store.appendMessage(sessionId, chatMsg);
      store.setStreaming(sessionId, null);
      store.setSessionStatus(sessionId, "running");

      // Start timer if not already started (for non-streaming tool calls)
      if (!store.streamingStartedAt.has(sessionId)) {
        store.setStreamingStats(sessionId, { startedAt: Date.now() });
      }

      // Extract tasks and changed files from tool_use content blocks
      if (msg.content?.length) {
        extractTasksFromBlocks(sessionId, msg.content);
        extractChangedFilesFromBlocks(sessionId, msg.content);
      }

      break;
    }

    case "stream_event": {
      const evt = data.event as Record<string, unknown>;
      if (evt && typeof evt === "object") {
        // message_start → mark generation start time
        if (evt.type === "message_start") {
          if (!store.streamingStartedAt.has(sessionId)) {
            store.setStreamingStats(sessionId, { startedAt: Date.now(), outputTokens: 0 });
          }
        }

        // content_block_delta → accumulate streaming text
        if (evt.type === "content_block_delta") {
          const delta = evt.delta as Record<string, unknown> | undefined;
          if (delta?.type === "text_delta" && typeof delta.text === "string") {
            const current = store.streaming.get(sessionId) || "";
            store.setStreaming(sessionId, current + delta.text);
          }
        }

        // message_delta → extract output token count
        if (evt.type === "message_delta") {
          const usage = (evt as { usage?: { output_tokens?: number } }).usage;
          if (usage?.output_tokens) {
            store.setStreamingStats(sessionId, { outputTokens: usage.output_tokens });
          }
        }
      }
      break;
    }

    case "result": {
      const r = data.data;
      const sessionUpdates: Partial<{ total_cost_usd: number; num_turns: number; context_used_percent: number; total_lines_added: number; total_lines_removed: number }> = {
        total_cost_usd: r.total_cost_usd,
        num_turns: r.num_turns,
      };
      // Forward lines changed if present
      if (typeof r.total_lines_added === "number") {
        sessionUpdates.total_lines_added = r.total_lines_added;
      }
      if (typeof r.total_lines_removed === "number") {
        sessionUpdates.total_lines_removed = r.total_lines_removed;
      }
      // Compute context % from modelUsage if available
      if (r.modelUsage) {
        for (const usage of Object.values(r.modelUsage)) {
          if (usage.contextWindow > 0) {
            const pct = Math.round(
              ((usage.inputTokens + usage.outputTokens) / usage.contextWindow) * 100
            );
            sessionUpdates.context_used_percent = Math.max(0, Math.min(pct, 100));
          }
        }
      }
      store.updateSession(sessionId, sessionUpdates);
      store.setStreaming(sessionId, null);
      store.setStreamingStats(sessionId, null);
      store.setSessionStatus(sessionId, "idle");
      // Play notification sound if enabled and tab is not focused
      if (!document.hasFocus() && store.notificationSound) {
        playNotificationSound();
      }
      if (r.is_error && r.errors?.length) {
        store.appendMessage(sessionId, {
          id: nextId(),
          role: "system",
          content: `Error: ${r.errors.join(", ")}`,
          timestamp: Date.now(),
        });
      }
      // Surface result text when assistant had thinking-only content (no text blocks)
      if (r.result && !r.is_error) {
        const msgs = store.messages.get(sessionId);
        const last = msgs?.[msgs.length - 1];
        if (last?.role === "assistant" && last.contentBlocks?.length &&
            !last.contentBlocks.some((b) => b.type === "text")) {
          store.updateLastAssistantMessage(sessionId, (msg) => ({
            ...msg,
            content: msg.content ? msg.content + "\n" + r.result : r.result!,
            contentBlocks: [...(msg.contentBlocks || []), { type: "text" as const, text: r.result! }],
          }));
        }
      }
      break;
    }

    case "permission_request": {
      store.addPermission(sessionId, data.request);
      // Also extract tasks and changed files from permission requests
      const req = data.request;
      if (req.tool_name && req.input) {
        const permBlocks = [{
          type: "tool_use" as const,
          id: req.tool_use_id,
          name: req.tool_name,
          input: req.input,
        }];
        extractTasksFromBlocks(sessionId, permBlocks);
        extractChangedFilesFromBlocks(sessionId, permBlocks);
      }
      break;
    }

    case "permission_cancelled": {
      store.removePermission(sessionId, data.request_id);
      break;
    }

    case "tool_progress": {
      // Could be used for progress indicators; ignored for now
      break;
    }

    case "tool_use_summary": {
      // Optional: add as system message
      break;
    }

    case "status_change": {
      if (data.status === "compacting") {
        store.setSessionStatus(sessionId, "compacting");
      } else if (data.status === "idle" && store.sessionStatus.get(sessionId) === "running") {
        // Don't downgrade from running to idle via status_change —
        // only the "result" message should clear "running" status.
        // This prevents flickering when the backend sends intermediate idle states.
      } else {
        store.setSessionStatus(sessionId, data.status);
      }
      break;
    }

    case "auth_status": {
      if (data.error) {
        store.appendMessage(sessionId, {
          id: nextId(),
          role: "system",
          content: `Auth error: ${data.error}`,
          timestamp: Date.now(),
        });
      }
      break;
    }

    case "error": {
      store.appendMessage(sessionId, {
        id: nextId(),
        role: "system",
        content: data.message,
        timestamp: Date.now(),
      });
      break;
    }

    case "guard_state": {
      store.setGuardEnabled(data.enabled);
      break;
    }

    case "audio_off": {
      stopWebRTC();
      break;
    }

    case "tts_muted": {
      const currentMode = store.audioMode;
      if (currentMode && currentMode !== "off") {
        store.setAudioMode(data.muted ? "in_only" : "in_out");
      }
      break;
    }

    case "voice_mode": {
      store.setVoiceMode((data as any).mode ?? null);
      break;
    }

    case "ring0_switch_ui": {
      // Ring0 requests switching to a specific session
      const targetId = data.sessionId;
      if (targetId && targetId !== store.currentSessionId) {
        // Disconnect from old session
        if (store.currentSessionId) {
          const oldSdk = store.sdkSessions.find((x) => x.sessionId === store.currentSessionId);
          if (oldSdk?.backendType !== "terminal") {
            disconnectSession(store.currentSessionId);
          }
        }
        store.setCurrentSession(targetId);
        const newSdk = store.sdkSessions.find((x) => x.sessionId === targetId);
        if (newSdk?.backendType !== "terminal") {
          connectSession(targetId);
        }
      }
      break;
    }

    case "node_switch": {
      // Hub notifies that the active node changed (e.g., via voice command)
      const newNodeId = data.nodeId as string;
      if (newNodeId && newNodeId !== store.activeNodeId) {
        // Save current session for this node before switching
        if (store.currentSessionId) {
          localStorage.setItem(`cc-node-session-${store.activeNodeId}`, store.currentSessionId);
          const oldSdk = store.sdkSessions.find((x) => x.sessionId === store.currentSessionId);
          if (oldSdk?.backendType !== "terminal") {
            disconnectSession(store.currentSessionId);
          }
        }
        store.setCurrentSession(null);
        store.setActiveNode(newNodeId);
      }
      break;
    }

    case "rpc_request": {
      const rpcId = data.id as string;
      const method = data.method as string;
      console.log(`[ws] RPC request: method=${method} id=${rpcId} session=${sessionId.slice(0,8)} params=`, (data as any).params);
      // Helper to send RPC responses via the source WS (for second screen) or global sockets
      const rpcSend = (msg: Record<string, unknown>) => {
        if (sourceWs?.readyState === WebSocket.OPEN) {
          sourceWs.send(JSON.stringify(msg));
        } else {
          sendToSession(sessionId, msg as BrowserOutgoingMessage);
        }
      };
      let response: Record<string, unknown>;
      try {

      if (method === "get_state") {
        const now = new Date();
        response = {
          type: "rpc_response",
          id: rpcId,
          result: {
            currentSessionId: store.currentSessionId,
            clientId: store.clientId,
            timestamp: Date.now(),
            dateTime: now.toISOString(),
            timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            timeZoneOffset: now.getTimezoneOffset(),
            locale: navigator.language,
            userAgent: navigator.userAgent,
            url: location.href,
          },
        };
      } else if (method === "get_location") {
        // Geolocation is async — send response when it resolves
        if (!navigator.geolocation) {
          response = { type: "rpc_response", id: rpcId, error: "Geolocation not supported" };
        } else {
          navigator.geolocation.getCurrentPosition(
            (pos) => {
              rpcSend({
                type: "rpc_response",
                id: rpcId,
                result: {
                  latitude: pos.coords.latitude,
                  longitude: pos.coords.longitude,
                  accuracy: pos.coords.accuracy,
                  altitude: pos.coords.altitude,
                  timestamp: pos.timestamp,
                },
              });
            },
            (err) => {
              const reason = err.code === 1 ? "permission_denied"
                : err.code === 2 ? "position_unavailable"
                : err.code === 3 ? "timeout"
                : "unknown";
              rpcSend({
                type: "rpc_response",
                id: rpcId,
                error: `Geolocation error: ${err.message}`,
                errorCode: reason,
              });
            },
            { enableHighAccuracy: false, timeout: 10000 },
          );
          break; // async — skip the synchronous send below
        }
      } else if (method === "send_notification") {
        const params = data.params as Record<string, string> | undefined;
        const ntitle = params?.title ?? "vibr8";
        const nbody = params?.body ?? "";
        if (!("Notification" in window)) {
          response = { type: "rpc_response", id: rpcId, error: "Notifications not supported", errorCode: "not_supported" };
        } else if (Notification.permission === "granted") {
          // Use Service Worker for mobile compatibility
          const sw = navigator.serviceWorker?.controller;
          if (sw) {
            sw.postMessage({ type: "show_notification", title: ntitle, body: nbody });
          } else {
            try { new Notification(ntitle, { body: nbody }); } catch { /* fallback failed, still report sent */ }
          }
          response = { type: "rpc_response", id: rpcId, result: { sent: true, permission: "granted" } };
        } else if (Notification.permission === "denied") {
          response = { type: "rpc_response", id: rpcId, error: "Notification permission denied by user", errorCode: "permission_denied" };
        } else {
          // Need to request — async
          Notification.requestPermission().then((perm) => {
            if (perm === "granted") {
              const sw2 = navigator.serviceWorker?.controller;
              if (sw2) {
                sw2.postMessage({ type: "show_notification", title: ntitle, body: nbody });
              } else {
                try { new Notification(ntitle, { body: nbody }); } catch { /* fallback */ }
              }
              rpcSend({ type: "rpc_response", id: rpcId, result: { sent: true, permission: "granted" } });
            } else {
              rpcSend({ type: "rpc_response", id: rpcId, error: "Notification permission denied by user", errorCode: "permission_denied" });
            }
          });
          break; // async
        }
      } else if (method === "get_visibility") {
        response = {
          type: "rpc_response",
          id: rpcId,
          result: {
            visible: document.visibilityState === "visible",
            state: document.visibilityState,
            hasFocus: document.hasFocus(),
          },
        };
      } else if (method === "read_clipboard") {
        if (!navigator.clipboard?.readText) {
          response = { type: "rpc_response", id: rpcId, error: "Clipboard API not supported", errorCode: "not_supported" };
        } else {
          navigator.clipboard.readText().then(
            (text) => {
              rpcSend({ type: "rpc_response", id: rpcId, result: { text } });
            },
            (err) => {
              const reason = (err as DOMException).name === "NotAllowedError" ? "permission_denied" : "read_failed";
              rpcSend({ type: "rpc_response", id: rpcId, error: `Clipboard read error: ${(err as Error).message}`, errorCode: reason });
            },
          );
          break; // async
        }
      } else if (method === "write_clipboard") {
        const text = (data.params as Record<string, string> | undefined)?.text ?? "";
        if (!navigator.clipboard?.writeText) {
          response = { type: "rpc_response", id: rpcId, error: "Clipboard API not supported", errorCode: "not_supported" };
        } else {
          navigator.clipboard.writeText(text).then(
            () => {
              rpcSend({ type: "rpc_response", id: rpcId, result: { written: true } });
            },
            (err) => {
              const reason = (err as DOMException).name === "NotAllowedError" ? "permission_denied" : "write_failed";
              rpcSend({ type: "rpc_response", id: rpcId, error: `Clipboard write error: ${(err as Error).message}`, errorCode: reason });
            },
          );
          break; // async
        }
      } else if (method === "open_url") {
        const url = (data.params as Record<string, string> | undefined)?.url;
        if (!url) {
          response = { type: "rpc_response", id: rpcId, error: "No url param provided", errorCode: "missing_param" };
        } else {
          window.open(url, "_blank");
          response = { type: "rpc_response", id: rpcId, result: { opened: true, url } };
        }
      } else if (method === "list_audio_devices" || method === "get_audio_devices") {
        if (!navigator.mediaDevices?.enumerateDevices) {
          response = { type: "rpc_response", id: rpcId, error: "enumerateDevices not supported", errorCode: "not_supported" };
        } else {
          navigator.mediaDevices.enumerateDevices().then(
            (devices) => {
              const inputs = devices
                .filter((d) => d.kind === "audioinput")
                .map((d) => ({ deviceId: d.deviceId, label: d.label || d.deviceId, groupId: d.groupId }));
              const outputs = devices
                .filter((d) => d.kind === "audiooutput")
                .map((d) => ({ deviceId: d.deviceId, label: d.label || d.deviceId, groupId: d.groupId }));

              // Active input: check WebRTC local stream audio track
              let activeInput: { deviceId: string; label: string } | null = null;
              const audio = getRemoteAudio();
              // Find active local stream from any WebRTC session
              const rtcSessions = (window as unknown as Record<string, unknown>).__v8_rtcSessions as Map<string, { localStream: MediaStream }> | undefined;
              if (rtcSessions) {
                for (const [, sess] of rtcSessions) {
                  const track = sess.localStream?.getAudioTracks()[0];
                  if (track) {
                    const settings = track.getSettings();
                    activeInput = { deviceId: settings.deviceId || "", label: track.label || settings.deviceId || "" };
                    break;
                  }
                }
              }

              // Active output: check setSinkId on remote audio element
              const activeOutputId = audio && (audio as unknown as Record<string, string>).sinkId;

              rpcSend({
                type: "rpc_response", id: rpcId,
                result: {
                  inputs,
                  outputs,
                  // Legacy compat: "devices" = outputs only
                  devices: outputs,
                  activeInput,
                  activeOutputDeviceId: activeOutputId || null,
                },
              });
            },
            (err) => {
              rpcSend({ type: "rpc_response", id: rpcId, error: `enumerateDevices error: ${(err as Error).message}` });
            },
          );
          break; // async
        }
      } else if (method === "set_audio_output") {
        const deviceId = (data.params as Record<string, string> | undefined)?.deviceId;
        if (!deviceId) {
          response = { type: "rpc_response", id: rpcId, error: "No deviceId param provided", errorCode: "missing_param" };
        } else {
          const audio = getRemoteAudio();
          if (!audio) {
            response = { type: "rpc_response", id: rpcId, error: "No active WebRTC audio", errorCode: "no_audio" };
          } else if (!(audio as any).setSinkId) {
            response = { type: "rpc_response", id: rpcId, error: "setSinkId not supported in this browser", errorCode: "not_supported" };
          } else {
            (audio as any).setSinkId(deviceId).then(
              () => {
                rpcSend({ type: "rpc_response", id: rpcId, result: { set: true, deviceId } });
              },
              (err: Error) => {
                rpcSend({ type: "rpc_response", id: rpcId, error: `setSinkId error: ${err.message}` });
              },
            );
            break; // async
          }
        }
      } else if (method === "set_audio_input") {
        const params = data.params as Record<string, string> | undefined;
        const requestedId = params?.deviceId;
        const requestedLabel = params?.label;
        if (!requestedId && !requestedLabel) {
          response = { type: "rpc_response", id: rpcId, error: "No deviceId or label param provided", errorCode: "missing_param" };
        } else {
          const rtcSessions = (window as unknown as Record<string, unknown>).__v8_rtcSessions as Map<string, { pc: RTCPeerConnection; localStream: MediaStream }> | undefined;
          let found = false;
          if (rtcSessions) {
            for (const [, sess] of rtcSessions) {
              found = true;
              // Always re-enumerate to get fresh device IDs (Android Chrome
              // rotates IDs for privacy — stale IDs cause getUserMedia to fail).
              navigator.mediaDevices.enumerateDevices().then(async (devices) => {
                const inputs = devices.filter((d) => d.kind === "audioinput");
                // Match by label first (stable), then by deviceId (may be stale)
                let target = requestedLabel
                  ? inputs.find((d) => d.label === requestedLabel)
                  : inputs.find((d) => d.deviceId === requestedId);
                // Fallback: case-insensitive label match
                if (!target && requestedLabel) {
                  const lower = requestedLabel.toLowerCase();
                  target = inputs.find((d) => d.label.toLowerCase() === lower);
                }
                // Fallback: try the requested deviceId even if not in fresh list
                const freshId = target?.deviceId || requestedId;
                if (!freshId) {
                  rpcSend({ type: "rpc_response", id: rpcId, error: `Device not found: ${requestedLabel || requestedId}`, errorCode: "not_found" });
                  return;
                }

                const targetLabel = target?.label;
                console.log("[ws] set_audio_input: requested:", { deviceId: requestedId, label: requestedLabel }, "→ fresh id:", freshId, "label:", targetLabel);

                try {
                  let newStream: MediaStream;
                  try {
                    newStream = await navigator.mediaDevices.getUserMedia({
                      audio: { deviceId: { exact: freshId }, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
                    });
                  } catch {
                    // exact failed — try ideal (Android Bluetooth workaround)
                    newStream = await navigator.mediaDevices.getUserMedia({
                      audio: { deviceId: { ideal: freshId }, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
                    });
                    // Verify ideal actually gave us the right device, not the default
                    const gotTrack = newStream.getAudioTracks()[0];
                    const gotSettings = gotTrack.getSettings();
                    if (gotSettings.deviceId !== freshId && gotTrack.label !== targetLabel) {
                      gotTrack.stop();
                      rpcSend({ type: "rpc_response", id: rpcId, error: `Device not available (got ${gotTrack.label || gotSettings.deviceId} instead)`, errorCode: "wrong_device" });
                      return;
                    }
                  }
                  const newTrack = newStream.getAudioTracks()[0];
                  const sender = sess.pc.getSenders().find((s) => s.track?.kind === "audio");
                  if (sender) await sender.replaceTrack(newTrack);
                  sess.localStream.getAudioTracks().forEach((t) => t.stop());
                  sess.localStream.getAudioTracks().forEach((t) => sess.localStream.removeTrack(t));
                  sess.localStream.addTrack(newTrack);
                  const settings = newTrack.getSettings();
                  const resolvedLabel = newTrack.label || targetLabel || settings.deviceId || "";
                  // Persist preference so startWebRTC can restore it
                  try { localStorage.setItem("cc-audio-input-label", resolvedLabel); } catch {}
                  rpcSend({ type: "rpc_response", id: rpcId, result: { set: true, deviceId: settings.deviceId, label: resolvedLabel } });
                  queryActiveInputDevice();
                } catch (err: unknown) {
                  rpcSend({ type: "rpc_response", id: rpcId, error: `getUserMedia error: ${(err as Error).message}` });
                }
              }).catch((err) => {
                rpcSend({ type: "rpc_response", id: rpcId, error: `enumerateDevices error: ${(err as Error).message}` });
              });
              break; // only one session
            }
          }
          if (!found) {
            response = { type: "rpc_response", id: rpcId, error: "No active WebRTC session", errorCode: "no_session" };
          } else {
            break; // async
          }
        }
      } else if (method === "show_content") {
        const params = data.params as Record<string, string> | undefined;
        const contentType = params?.type ?? "markdown";
        const content = params?.content ?? "";
        const filename = params?.filename;
        store.setSecondScreenContent({ type: contentType, content, ...(filename && { filename }) });
        response = { type: "rpc_response", id: rpcId, result: { shown: true, type: contentType } };
      } else if (method === "mirror_session") {
        const params = data.params as Record<string, string> | undefined;
        const sid = params?.sessionId ?? null;
        store.setMirroredSessionId(sid);
        store.setSecondScreenContent(null);
        response = { type: "rpc_response", id: rpcId, result: { mirroring: true, sessionId: sid } };
      } else if (method === "set_scale") {
        const params = data.params as Record<string, number> | undefined;
        const current = store.secondScreenScale;
        let newScale: number;
        if (params?.delta !== undefined) {
          newScale = current + params.delta;
        } else if (params?.scale !== undefined) {
          newScale = params.scale;
        } else {
          newScale = current;
        }
        newScale = Math.max(0.5, Math.min(3.0, newScale));
        store.setSecondScreenScale(newScale);
        response = { type: "rpc_response", id: rpcId, result: { scale: newScale } };
      } else if (method === "set_tv_safe") {
        const params = data.params as Record<string, unknown> | undefined;
        const enabled = params?.enabled as boolean | undefined;
        const pct = params?.padding_percent as number | undefined;
        let padding: number;
        if (enabled === false) {
          padding = 0;
        } else if (pct != null && pct > 0) {
          padding = pct;
        } else if (enabled === true) {
          padding = store.secondScreenTvSafe > 0 ? store.secondScreenTvSafe : 2.5;
        } else {
          // toggle
          padding = store.secondScreenTvSafe > 0 ? 0 : 2.5;
        }
        store.setSecondScreenTvSafe(padding);
        response = { type: "rpc_response", id: rpcId, result: { tvSafe: padding > 0, paddingPercent: padding } };
      } else if (method === "set_name") {
        const params = data.params as Record<string, string> | undefined;
        const name = params?.name ?? null;
        store.setSecondScreenClientName(name);
        response = { type: "rpc_response", id: rpcId, result: { name } };
      } else if (method === "set_dark_mode") {
        const params = data.params as Record<string, unknown> | undefined;
        const enabled = params?.enabled as boolean | undefined;
        const newVal = enabled ?? !store.secondScreenDarkMode;
        store.setSecondScreenDarkMode(newVal);
        response = { type: "rpc_response", id: rpcId, result: { darkMode: newVal } };
      } else if (method === "get_device_info") {
        response = {
          type: "rpc_response", id: rpcId,
          result: {
            screenWidth: window.innerWidth,
            screenHeight: window.innerHeight,
            devicePixelRatio: window.devicePixelRatio,
            userAgent: navigator.userAgent,
            platform: navigator.platform,
            language: navigator.language,
            colorScheme: window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light",
            online: navigator.onLine,
            touchSupport: navigator.maxTouchPoints > 0,
            scale: store.secondScreenScale,
            tvSafe: store.secondScreenTvSafe > 0,
            tvSafePaddingPercent: store.secondScreenTvSafe,
            darkMode: store.secondScreenDarkMode,
          },
        };
      } else if (method === "clear_content") {
        store.setSecondScreenContent(null);
        store.setMirroredSessionId(null);
        response = { type: "rpc_response", id: rpcId, result: { cleared: true } };
      } else if (method === "bring_to_foreground") {
        const cap = (window as unknown as Record<string, unknown>).Capacitor as
          | { Plugins?: Record<string, { bringToForeground?: () => Promise<void> }> }
          | undefined;
        const plugin = cap?.Plugins?.BringToForeground;
        if (!plugin?.bringToForeground) {
          response = { type: "rpc_response", id: rpcId, error: "BringToForeground plugin not available", errorCode: "not_supported" };
        } else {
          plugin.bringToForeground().then(
            () => { rpcSend({ type: "rpc_response", id: rpcId, result: { foreground: true } }); },
            (err: Error) => { rpcSend({ type: "rpc_response", id: rpcId, error: `BringToForeground error: ${err.message}` }); },
          );
          break; // async
        }
      } else if (method === "capture_screenshot") {
        const params = data.params as Record<string, unknown> | undefined;
        const format = (params?.format as string) ?? "png";
        const quality = (params?.quality as number) ?? 0.8;

        import("html2canvas").then(({ default: html2canvas }) => {
          html2canvas(document.body, {
            useCORS: true,
            scale: window.devicePixelRatio || 1,
            logging: false,
          }).then((canvas) => {
            const mimeType = format === "jpeg" ? "image/jpeg" : "image/png";
            const dataUrl = canvas.toDataURL(mimeType, quality);
            const base64 = dataUrl.split(",")[1];
            rpcSend({
              type: "rpc_response",
              id: rpcId,
              result: {
                image: base64,
                mime: mimeType,
                width: canvas.width,
                height: canvas.height,
              },
            });
          }).catch((err: Error) => {
            rpcSend({ type: "rpc_response", id: rpcId, error: `Screenshot failed: ${err.message}` });
          });
        }).catch((err: Error) => {
          rpcSend({ type: "rpc_response", id: rpcId, error: `Failed to load html2canvas: ${err.message}` });
        });
        break; // async
      } else {
        response = { type: "rpc_response", id: rpcId, error: `unknown method: ${method}` };
      }

      console.log(`[ws] RPC response: method=${method} id=${rpcId}`, response!);
      // Use the source WebSocket if provided (e.g., second screen's dedicated WS)
      if (sourceWs?.readyState === WebSocket.OPEN) {
        sourceWs.send(JSON.stringify(response));
      } else {
        sendToSession(sessionId, response as BrowserOutgoingMessage);
      }
      } catch (e) {
        console.error(`[ws] RPC handler error: method=${method} id=${rpcId}`, e);
        rpcSend({ type: "rpc_response", id: rpcId, error: `RPC handler error: ${(e as Error).message}` });
      }
      break;
    }

    case "cli_disconnected": {
      console.log(`[ws] cli_disconnected → cliConnected=false session=${sessionId.slice(0,8)}`);
      store.setCliConnected(sessionId, false);
      store.setSessionStatus(sessionId, null);
      break;
    }

    case "cli_connected": {
      console.log(`[ws] cli_connected → cliConnected=true session=${sessionId.slice(0,8)}`);
      store.setCliConnected(sessionId, true);
      break;
    }

    case "user_message": {
      store.appendMessage(sessionId, {
        id: nextId(),
        role: "user",
        content: data.content,
        timestamp: Date.now(),
        eventMeta: data.eventMeta,
      });
      break;
    }

    case "session_name_update": {
      // User renames always apply; auto-names only overwrite random "Adj Noun" names
      const currentName = store.sessionNames.get(sessionId);
      const isRandomName = currentName && /^[A-Z][a-z]+ [A-Z][a-z]+$/.test(currentName);
      if (data.userRenamed || !currentName || isRandomName) {
        store.setSessionName(sessionId, data.name);
        if (!data.userRenamed) store.markRecentlyRenamed(sessionId);
      }
      break;
    }

    case "message_history": {
      console.log(`[ws] message_history session=${sessionId.slice(0,8)} count=${data.messages?.length} existingCount=${(store.messages.get(sessionId) || []).length}`);
      const chatMessages: ChatMessage[] = [];
      for (const histMsg of data.messages) {
        if (histMsg.type === "user_message") {
          chatMessages.push({
            id: nextId(),
            role: "user",
            content: histMsg.content,
            timestamp: histMsg.timestamp,
            eventMeta: histMsg.eventMeta,
          });
        } else if (histMsg.type === "assistant") {
          const msg = histMsg.message;
          const textContent = extractTextFromBlocks(msg.content);
          chatMessages.push({
            id: msg.id,
            role: "assistant",
            content: textContent,
            contentBlocks: msg.content,
            timestamp: histMsg.timestamp || Date.now(),
            parentToolUseId: histMsg.parent_tool_use_id,
            model: msg.model,
            stopReason: msg.stop_reason,
          });
          // Also extract tasks and changed files from history
          if (msg.content?.length) {
            extractTasksFromBlocks(sessionId, msg.content);
            extractChangedFilesFromBlocks(sessionId, msg.content);
          }
        } else if (histMsg.type === "result") {
          const r = histMsg.data;
          if (r.is_error && r.errors?.length) {
            chatMessages.push({
              id: nextId(),
              role: "system",
              content: `Error: ${r.errors.join(", ")}`,
              timestamp: histMsg.timestamp || Date.now(),
            });
          }
          // Surface result text when preceding assistant had thinking-only content
          if (r.result && !r.is_error && chatMessages.length > 0) {
            const last = chatMessages[chatMessages.length - 1];
            if (last.role === "assistant" && last.contentBlocks?.length &&
                !last.contentBlocks.some((b) => b.type === "text")) {
              last.content = last.content ? last.content + "\n" + r.result : r.result;
              last.contentBlocks = [...(last.contentBlocks || []), { type: "text" as const, text: r.result }];
            }
          }
        }
      }
      if (chatMessages.length > 0) {
        const existing = store.messages.get(sessionId) || [];
        // Only replace if history has at least as many messages as current state,
        // or if the current state is empty (initial connect). This prevents a race
        // condition where live messages (e.g., tool_use) are lost by a stale history replay.
        if (existing.length === 0 || chatMessages.length >= existing.length) {
          store.setMessages(sessionId, chatMessages);
        }
      }
      if (data.archivedMessageCount) {
        store.setArchivedCount(sessionId, data.archivedMessageCount);
      }
      break;
    }
  }
}

export function connectSession(sessionId: string) {
  if (sockets.has(sessionId)) {
    console.log(`[ws] connectSession SKIP (already connected) session=${sessionId.slice(0,8)}`);
    return;
  }
  console.log(`[ws] connectSession NEW session=${sessionId.slice(0,8)} totalSockets=${sockets.size}`);

  const store = useStore.getState();
  store.setConnectionStatus(sessionId, "connecting");

  const ws = new WebSocket(getWsUrl(sessionId));
  sockets.set(sessionId, ws);

  let keepaliveInterval: ReturnType<typeof setInterval> | null = null;

  ws.onopen = () => {
    console.log(`[ws] onopen → connectionStatus=connected session=${sessionId.slice(0,8)}`);
    const s = useStore.getState();
    s.setConnectionStatus(sessionId, "connected");
    // Clear reconnect state on successful connection.
    clearReconnectState(sessionId);
    // Application-level keepalive to prevent proxy timeouts
    keepaliveInterval = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "ping" }));
      }
    }, 15000);
    // Report device info for client metadata (fire-and-forget)
    fetch(`/api/clients/${encodeURIComponent(s.clientId)}/device-info`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        screenWidth: window.innerWidth,
        screenHeight: window.innerHeight,
        devicePixelRatio: window.devicePixelRatio,
        userAgent: navigator.userAgent,
        platform: navigator.platform,
        language: navigator.language,
        touchSupport: navigator.maxTouchPoints > 0,
      }),
    }).catch(() => {});
  };

  ws.onmessage = (event) => handleMessage(sessionId, event);

  ws.onclose = () => {
    console.log(`[ws] onclose → connectionStatus=disconnected, cliConnected=false session=${sessionId.slice(0,8)} current=${useStore.getState().currentSessionId?.slice(0,8) ?? 'null'}`);
    if (keepaliveInterval) { clearInterval(keepaliveInterval); keepaliveInterval = null; }
    sockets.delete(sessionId);
    const s = useStore.getState();
    s.setConnectionStatus(sessionId, "disconnected");
    s.setCliConnected(sessionId, false);
    s.setSessionStatus(sessionId, null);
    scheduleReconnect(sessionId);
  };

  ws.onerror = () => {
    ws.close();
  };
}

function clearReconnectState(sessionId: string) {
  const timer = reconnectTimers.get(sessionId);
  if (timer) {
    clearTimeout(timer);
    reconnectTimers.delete(sessionId);
  }
  reconnectStartTimes.delete(sessionId);
  const s = useStore.getState();
  s.setReconnecting(sessionId, false);
  s.setReconnectGaveUp(sessionId, false);
}

function scheduleReconnect(sessionId: string) {
  if (reconnectTimers.has(sessionId)) return;

  // Terminal sessions don't use WsBridge — skip reconnect
  const sdk = useStore.getState().sdkSessions.find((x) => x.sessionId === sessionId);
  if (sdk?.backendType === "terminal") return;

  // Record when reconnection attempts started.
  if (!reconnectStartTimes.has(sessionId)) {
    reconnectStartTimes.set(sessionId, Date.now());
    useStore.getState().setReconnecting(sessionId, true);
  }

  const timer = setTimeout(() => {
    reconnectTimers.delete(sessionId);
    const store = useStore.getState();

    // Check if we've exceeded the timeout.
    const startTime = reconnectStartTimes.get(sessionId) ?? Date.now();
    if (Date.now() - startTime >= RECONNECT_TIMEOUT_MS) {
      reconnectStartTimes.delete(sessionId);
      store.setReconnecting(sessionId, false);
      store.setReconnectGaveUp(sessionId, true);
      return;
    }

    if (store.currentSessionId === sessionId || store.sessions.has(sessionId)) {
      connectSession(sessionId);
    }
  }, RECONNECT_INTERVAL_MS);
  reconnectTimers.set(sessionId, timer);
}

export function cancelReconnect(sessionId: string) {
  clearReconnectState(sessionId);
  useStore.getState().setReconnectGaveUp(sessionId, true);
}

export function manualReconnect(sessionId: string) {
  clearReconnectState(sessionId);
  // Force-close and remove any stale socket so connectSession doesn't bail
  const old = sockets.get(sessionId);
  if (old) {
    old.onclose = null;
    old.onerror = null;
    old.close();
    sockets.delete(sessionId);
  }
  reconnectStartTimes.set(sessionId, Date.now());
  useStore.getState().setReconnecting(sessionId, true);
  connectSession(sessionId);
}

// Immediately reconnect when returning from background (mobile app switch, screen lock)
if (!_w.__v8_visibilityHandler) {
  _w.__v8_visibilityHandler = true;
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    const store = useStore.getState();
    const sid = store.currentSessionId;
    if (!sid) return;
    const ws = sockets.get(sid);
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      // Reset give-up state and reconnect immediately
      clearReconnectState(sid);
      connectSession(sid);
    }
  });
}

export function disconnectSession(sessionId: string) {
  console.log(`[ws] disconnectSession session=${sessionId.slice(0,8)} hasSocket=${sockets.has(sessionId)}`);
  clearReconnectState(sessionId);
  const ws = sockets.get(sessionId);
  if (ws) {
    ws.close();
    sockets.delete(sessionId);
  }
  processedToolUseIds.delete(sessionId);
  taskCounters.delete(sessionId);
}

export function disconnectAll() {
  for (const [id] of sockets) {
    disconnectSession(id);
  }
}

export function waitForConnection(sessionId: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const check = setInterval(() => {
      const ws = sockets.get(sessionId);
      if (ws?.readyState === WebSocket.OPEN) {
        clearInterval(check);
        clearTimeout(timeout);
        resolve();
      }
    }, 50);
    const timeout = setTimeout(() => {
      clearInterval(check);
      reject(new Error("Connection timeout"));
    }, 10000);
  });
}

export function sendToSession(sessionId: string, msg: BrowserOutgoingMessage) {
  const ws = sockets.get(sessionId);
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
  }
}

