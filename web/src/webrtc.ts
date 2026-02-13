import { useStore } from "./store.js";
import { api } from "./api.js";

export interface WebRTCSession {
  pc: RTCPeerConnection;
  localStream: MediaStream;
  remoteAudio: HTMLAudioElement;
}

const rtcSessions = new Map<string, WebRTCSession>();

export async function startWebRTC(sessionId: string): Promise<void> {
  if (rtcSessions.has(sessionId)) return;

  const localStream = await navigator.mediaDevices.getUserMedia({ audio: true });

  // Start with mic muted
  for (const track of localStream.getAudioTracks()) {
    track.enabled = false;
  }

  // No STUN/TURN — local network only
  const pc = new RTCPeerConnection({ iceServers: [] });

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
  };

  // Monitor connection state changes
  pc.onconnectionstatechange = () => {
    useStore.getState().setWebRTCStatus(sessionId, pc.connectionState);

    if (pc.connectionState === "failed" || pc.connectionState === "closed") {
      stopWebRTC(sessionId);
    }
  };

  // Create and set local offer
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);

  // Exchange SDP with the backend
  const answer = await api.webrtcOffer(sessionId, {
    sdp: offer.sdp!,
    type: offer.type,
  });

  // Set the remote answer
  await pc.setRemoteDescription(
    new RTCSessionDescription({
      sdp: answer.sdp,
      type: answer.type as RTCSdpType,
    }),
  );

  // Store the session
  rtcSessions.set(sessionId, { pc, localStream, remoteAudio });

  // Update store to reflect audio is enabled
  useStore.getState().setAudioEnabled(sessionId, true);
}

export function setMicEnabled(sessionId: string, enabled: boolean): void {
  const session = rtcSessions.get(sessionId);
  if (!session) return;

  for (const track of session.localStream.getAudioTracks()) {
    track.enabled = enabled;
  }

  useStore.getState().setIsRecording(sessionId, enabled);
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
  store.setAudioEnabled(sessionId, false);
  store.setIsRecording(sessionId, false);
  store.setWebRTCStatus(sessionId, null);
}

export function isWebRTCActive(sessionId: string): boolean {
  return rtcSessions.has(sessionId);
}
