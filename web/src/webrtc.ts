import { useStore } from "./store.js";
import { api } from "./api.js";

export interface WebRTCSession {
  pc: RTCPeerConnection;
  localStream: MediaStream;
  remoteAudio: HTMLAudioElement;
}

// Store on window to survive Vite HMR module reloads
const _w = window as unknown as Record<string, unknown>;
const rtcSessions = (_w.__v8_rtcSessions ??= new Map<string, WebRTCSession>()) as Map<string, WebRTCSession>;

export async function startWebRTC(sessionId: string): Promise<void> {
  if (rtcSessions.has(sessionId)) return;

  // Only one WebRTC session at a time — close any existing one first.
  for (const [existingId] of rtcSessions) {
    stopWebRTC(existingId);
  }

  // Show blinking "connecting" state immediately
  const store0 = useStore.getState();
  store0.setAudioSessionId(sessionId);
  store0.setAudioMode("connecting");

  // Get mic permission — restore saved device preference if available
  const audioConstraints: MediaTrackConstraints = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  const savedInputLabel = localStorage.getItem("cc-audio-input-label");
  if (savedInputLabel) {
    // Get fresh device list and match by label (deviceIds rotate on Android)
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      const match = devices.find((d) => d.kind === "audioinput" && d.label === savedInputLabel);
      if (match) audioConstraints.deviceId = { exact: match.deviceId };
    } catch { /* fall through to default */ }
  }
  const localStream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints });

  // Fetch ICE servers from backend (STUN/TURN config)
  let iceServers: RTCIceServer[] = [];
  try {
    const config = await api.getIceServers();
    iceServers = config.iceServers;
  } catch {
    // Fall back to empty (local network only)
  }

  const pc = new RTCPeerConnection({ iceServers });

  // Add local audio track to the peer connection
  const audioTrack = localStream.getAudioTracks()[0];
  pc.addTrack(audioTrack, localStream);

  // Create the remote audio element for playback
  const remoteAudio = new Audio();
  remoteAudio.autoplay = true;

  // Handle incoming remote tracks
  pc.ontrack = (event) => {
    if (event.streams[0]) {
      remoteAudio.srcObject = event.streams[0];
    } else {
      remoteAudio.srcObject = new MediaStream([event.track]);
    }
    // Ensure playback starts (may be blocked by autoplay policy)
    remoteAudio.play().catch(() => {});
  };

  // Monitor connection state changes
  pc.onconnectionstatechange = () => {
    useStore.getState().setWebRTCStatus(pc.connectionState);

    if (pc.connectionState === "connected") {
      // Connection established — detect transport type (direct vs relay)
      detectTransportType(pc);
      const store = useStore.getState();
      store.setAudioMode("in_out");
      store.setIsRecording(true);
      // Restore persisted guard state (defaults to true for fresh sessions)
      const guard = store.guardEnabled;
      api.setGuard(sessionId, guard).catch(() => {});
    } else if (pc.connectionState === "failed" || pc.connectionState === "closed") {
      stopWebRTC(sessionId);
    }
  };

  // Create and set local offer
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);

  // Wait for ICE gathering to complete so relay (TURN) candidates are
  // included in the offer SDP.  Without trickle ICE signaling, candidates
  // not in the initial SDP are lost.
  if (pc.iceGatheringState !== "complete") {
    await new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, 10_000); // 10s timeout
      pc.onicegatheringstatechange = () => {
        if (pc.iceGatheringState === "complete") {
          clearTimeout(timer);
          resolve();
        }
      };
    });
  }

  // Exchange SDP with the backend (now includes all candidates)
  const answer = await api.webrtcOffer(
    sessionId,
    { sdp: pc.localDescription!.sdp, type: pc.localDescription!.type },
    useStore.getState().clientId,
  );

  // Set the remote answer
  await pc.setRemoteDescription(
    new RTCSessionDescription({
      sdp: answer.sdp,
      type: answer.type as RTCSdpType,
    }),
  );

  // Store the session
  rtcSessions.set(sessionId, { pc, localStream, remoteAudio });

  // Detect active audio input device
  queryActiveInputDevice();
}

async function detectTransportType(
  pc: RTCPeerConnection,
): Promise<void> {
  try {
    const stats = await pc.getStats();
    let transport: "direct" | "relay" = "direct";
    stats.forEach((report) => {
      if (
        report.type === "candidate-pair" &&
        report.state === "succeeded" &&
        report.nominated
      ) {
        // Look up the local candidate to check its type
        const localCandidate = stats.get(report.localCandidateId);
        if (localCandidate?.candidateType === "relay") {
          transport = "relay";
        }
      }
    });
    useStore.getState().setWebRTCTransport(transport);
  } catch {
    // Stats API not available, assume direct
    useStore.getState().setWebRTCTransport("direct");
  }
}

export function toggleGuard(sessionId: string): void {
  const current = useStore.getState().guardEnabled;
  useStore.getState().setGuardEnabled(!current);
  api.setGuard(sessionId, !current).catch(() => {});
}

export function setAudioInOnly(sessionId: string): void {
  api.setTtsMuted(sessionId, true).catch(() => {});
  useStore.getState().setAudioMode("in_only");
}

export function setAudioInOut(sessionId: string): void {
  api.setTtsMuted(sessionId, false).catch(() => {});
  useStore.getState().setAudioMode("in_out");
}

export function stopWebRTC(sessionId: string): void {
  const session = rtcSessions.get(sessionId);
  if (!session) return;

  rtcSessions.delete(sessionId);

  // Stop all local media tracks
  for (const track of session.localStream.getTracks()) {
    track.stop();
  }

  // Close the peer connection
  session.pc.close();

  // Detach remote audio
  session.remoteAudio.srcObject = null;

  // Update store
  const store = useStore.getState();
  store.setAudioSessionId(null);
  store.setAudioMode("off");
  store.setIsRecording(false);
  store.setWebRTCStatus(null);
  store.setWebRTCTransport(null);
  store.setVoiceMode(null);
  store.setActiveAudioInputLabel(null);
}

export function isWebRTCActive(sessionId: string): boolean {
  return rtcSessions.has(sessionId);
}

/** Return the active remote audio element (for setSinkId, etc.), or null. */
export function getRemoteAudio(): HTMLAudioElement | null {
  for (const [, session] of rtcSessions) {
    return session.remoteAudio;
  }
  return null;
}

// ── Audio input device detection ──────────────────────────────────────────

/** Query the active audio input device label and update the store.
 *  Resolves "default" deviceId to the actual physical device name. */
export async function queryActiveInputDevice(): Promise<void> {
  try {
    let track: MediaStreamTrack | undefined;
    for (const [, session] of rtcSessions) {
      track = session.localStream.getAudioTracks()[0];
      break;
    }
    if (!track) {
      useStore.getState().setActiveAudioInputLabel(null);
      return;
    }

    const settings = track.getSettings();
    const activeDeviceId = settings.deviceId;
    const trackLabel = track.label;

    const devices = await navigator.mediaDevices.enumerateDevices();
    const inputs = devices.filter((d) => d.kind === "audioinput");
    let label: string | null = null;

    console.log("[webrtc] queryActiveInputDevice — track:", { label: trackLabel, deviceId: activeDeviceId, groupId: settings.groupId });
    console.log("[webrtc] queryActiveInputDevice — inputs:", inputs.map((d) => `"${d.label}" (id=${d.deviceId.slice(0, 12)}, group=${d.groupId.slice(0, 8)})`));

    if (activeDeviceId && activeDeviceId !== "default") {
      // Real device ID — direct match
      const match = inputs.find((d) => d.deviceId === activeDeviceId);
      label = match?.label || trackLabel || null;
    } else {
      // deviceId is "default" or missing — resolve to actual device
      const trackGroupId = settings.groupId;

      // Strategy 1: track's own groupId → match against enumerated devices.
      // The track's groupId reflects the ACTUAL physical device even when
      // deviceId is "default".
      if (trackGroupId) {
        const match = inputs.find(
          (d) => d.deviceId !== "default" && d.deviceId !== "communications" && d.groupId === trackGroupId,
        );
        if (match?.label) {
          label = match.label;
          console.log("[webrtc] resolved via track groupId match:", label);
        }
      }

      // Strategy 2: track.label (physical device name on some platforms)
      if (!label && trackLabel && trackLabel !== "Default") {
        label = trackLabel;
        console.log("[webrtc] resolved via track.label:", label);
      }

      // Strategy 3: "default" entry's label may contain the real name
      if (!label) {
        const defaultEntry = inputs.find((d) => d.deviceId === "default");
        if (defaultEntry?.label && defaultEntry.label !== "Default") {
          label = defaultEntry.label;
          console.log("[webrtc] resolved via default entry label:", label);
        }

        // Strategy 4: "default" entry's groupId → match real device
        if (!label && defaultEntry?.groupId) {
          const real = inputs.find(
            (d) => d.deviceId !== "default" && d.groupId === defaultEntry.groupId,
          );
          if (real?.label) {
            label = real.label;
            console.log("[webrtc] resolved via default entry groupId:", label);
          }
        }
      }

      // Strategy 5: "communications" device (Windows)
      if (!label) {
        const commEntry = inputs.find((d) => d.deviceId === "communications");
        if (commEntry?.label && commEntry.label !== "Default") {
          label = commEntry.label;
        }
      }

      // Strategy 6 (last resort): first non-synthetic device — unreliable,
      // may pick the wrong device
      if (!label) {
        const firstReal = inputs.find(
          (d) => d.deviceId !== "default" && d.deviceId !== "communications" && d.label,
        );
        if (firstReal?.label) {
          label = firstReal.label;
          console.log("[webrtc] resolved via last resort (first device):", label);
        }
      }
    }

    // Strip "Default - " prefix some browsers prepend
    if (label) {
      label = label.replace(/^Default\s*[-–—]\s*/i, "");
    }

    console.log("[webrtc] queryActiveInputDevice — resolved:", label);
    useStore.getState().setActiveAudioInputLabel(label || null);
  } catch {
    useStore.getState().setActiveAudioInputLabel(null);
  }
}

// Listen for device changes (Bluetooth connect/disconnect, etc.)
if (typeof navigator !== "undefined" && navigator.mediaDevices) {
  navigator.mediaDevices.addEventListener("devicechange", () => {
    // Only re-query if audio is active
    if (rtcSessions.size > 0) {
      queryActiveInputDevice();
    }
  });
}

// ── Playground WebRTC ──────────────────────────────────────────────────────

interface PlaygroundSession {
  pc: RTCPeerConnection;
  localStream: MediaStream;
  ws: WebSocket;
  sessionId: string;
}

const _pw = window as unknown as Record<string, unknown>;
let playgroundSession = (_pw.__v8_playground ??= null) as PlaygroundSession | null;

export async function startPlaygroundWebRTC(profileId?: string): Promise<string> {
  if (playgroundSession) {
    await stopPlaygroundWebRTC();
  }

  const store = useStore.getState();
  const clientId = store.clientId;
  const sessionId = `playground-${clientId}`;

  store.setPlaygroundActive(true);
  store.setPlaygroundSessionId(sessionId);
  store.clearPlaygroundSegments();

  const localStream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });

  let iceServers: RTCIceServer[] = [];
  try {
    const config = await api.getIceServers();
    iceServers = config.iceServers;
  } catch {
    // Fall back to empty
  }

  const pc = new RTCPeerConnection({ iceServers });
  const audioTrack = localStream.getAudioTracks()[0];
  pc.addTrack(audioTrack, localStream);

  // We don't need remote audio for playground, but create a dummy handler
  pc.ontrack = () => {};

  // Connect playground WebSocket
  const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${wsProto}//${window.location.host}/ws/playground/${clientId}`;
  const ws = new WebSocket(wsUrl);

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      const s = useStore.getState();
      if (data.type === "voice_level") {
        s.setPlaygroundLevel(data.rmsDb);
      } else if (data.type === "voice_activity") {
        s.setPlaygroundVadActive(data.active);
      } else if (data.type === "segment") {
        s.addPlaygroundSegment({
          transcript: data.transcript,
          timeBegin: data.timeBegin,
          timeEnd: data.timeEnd,
          segmentId: data.segmentId,
        });
      }
    } catch {
      // ignore
    }
  };

  ws.onclose = () => {
    if (playgroundSession?.sessionId === sessionId) {
      stopPlaygroundWebRTC();
    }
  };

  // Wait for WS to open
  await new Promise<void>((resolve, reject) => {
    ws.onopen = () => resolve();
    ws.onerror = () => reject(new Error("Playground WS failed"));
    setTimeout(() => reject(new Error("Playground WS timeout")), 5000);
  });

  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);

  // Wait for ICE gathering
  if (pc.iceGatheringState !== "complete") {
    await new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, 10_000);
      pc.onicegatheringstatechange = () => {
        if (pc.iceGatheringState === "complete") {
          clearTimeout(timer);
          resolve();
        }
      };
    });
  }

  const answer = await api.webrtcOffer(
    sessionId,
    { sdp: pc.localDescription!.sdp, type: pc.localDescription!.type },
    clientId,
    { playground: true, profileId },
  );

  await pc.setRemoteDescription(
    new RTCSessionDescription({ sdp: answer.sdp, type: answer.type as RTCSdpType }),
  );

  playgroundSession = { pc, localStream, ws, sessionId };
  (_pw.__v8_playground as unknown) = playgroundSession;

  // Mute main session(s) so playground testing doesn't reach the agent
  for (const [, session] of rtcSessions) {
    for (const track of session.localStream.getAudioTracks()) {
      track.enabled = false;
    }
  }

  return sessionId;
}

export function sendPlaygroundParams(params: Record<string, number | string>) {
  if (!playgroundSession) return;
  const msg = JSON.stringify({
    type: "update_params",
    sessionId: playgroundSession.sessionId,
    ...params,
  });
  if (playgroundSession.ws.readyState === WebSocket.OPEN) {
    playgroundSession.ws.send(msg);
  }
}

export async function stopPlaygroundWebRTC(): Promise<void> {
  const session = playgroundSession;
  if (!session) return;

  playgroundSession = null;
  (_pw.__v8_playground as unknown) = null;

  for (const track of session.localStream.getTracks()) {
    track.stop();
  }
  session.pc.close();
  if (session.ws.readyState === WebSocket.OPEN || session.ws.readyState === WebSocket.CONNECTING) {
    session.ws.close();
  }

  // Unmute main session(s)
  for (const [, session] of rtcSessions) {
    for (const track of session.localStream.getAudioTracks()) {
      track.enabled = true;
    }
  }

  const store = useStore.getState();
  store.setPlaygroundActive(false);
  // Keep playgroundSessionId and playgroundSegments so clips remain visible/playable
  store.setPlaygroundLevel(-60);
  store.setPlaygroundVadActive(false);
}

export function isPlaygroundActive(): boolean {
  return playgroundSession !== null;
}
