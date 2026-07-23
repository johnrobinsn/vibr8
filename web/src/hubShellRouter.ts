// Router for messages the hub pushes over /ws/hub-shell/{clientId}.
//
// The hub-shell WS carries a small, deliberately node-agnostic set of
// pushes (contract §B design principles): voice pipeline state, node
// selection, node metadata. Any *session*-scoped push (e.g.
// ring0_switch_ui) travels node → iframe directly and must NOT be
// dispatched here — routing it through the shell would violate the
// shell-contract-stays-small principle codified in
// docs/hub-node-contract-v1.md.
//
// Extracted from App.tsx so the routing table is a single source of
// truth and can be unit-tested without mounting React. Any new push
// type the server sends over /ws/hub-shell must appear in
// HUB_SHELL_HANDLERS or dispatchHubShellMessage will warn on unknown
// type — a visible signal in dev that the front end forgot to route
// a new message.

import { useStore } from "./store.js";
import { applyLocalNodeSwitch } from "./ws.js";
import { PINNED_NODE } from "./pinnedNode.js";

type HubShellMessage = { type: string } & Record<string, unknown>;
type HubShellHandler = (msg: HubShellMessage) => void;

export const HUB_SHELL_HANDLERS: Record<string, HubShellHandler> = {
  voice_mode: (d) => {
    useStore.getState().setVoiceMode((d.mode as string) ?? null);
  },
  node_title: (d) => {
    useStore.getState().setNodeTitle(
      d.nodeId as string,
      (d.text as string) ?? "",
    );
  },
  ring0_switch_node: (d) => {
    // Shell owns node selection / iframe URL, so switch-node lands
    // here. Contrast: switch_ui does NOT — it goes node → iframe.
    //
    // Pinned tab: refuse the switch client-side so we don't trigger
    // the transient churn (session-state clear, doubled active-node
    // POST, React re-force during render) that would otherwise fire
    // before App.tsx's pin guard snaps activeNodeId back. The hub
    // also refuses at the /activate route (see hub_browser_bridge's
    // is_client_pinned), which is what surfaces the honest error to
    // Ring0 — this branch is the client-side belt to that server-side
    // suspenders.
    if (PINNED_NODE) {
      console.warn(
        `[shell-ws] ring0_switch_node to ${(d.nodeId as string)?.slice(0, 8)} ignored — tab is pinned to '${PINNED_NODE}'`,
      );
      return;
    }
    applyLocalNodeSwitch(d.nodeId as string);
  },
};

/** Parse & dispatch one message from the hub-shell WS. Returns true
 * when a registered handler ran, false when the payload was
 * malformed or the type was not registered. Logs a warning on
 * unregistered types so gaps become visible in dev. */
export function dispatchHubShellMessage(raw: string): boolean {
  let d: HubShellMessage;
  try {
    d = JSON.parse(raw) as HubShellMessage;
  } catch {
    return false;
  }
  if (!d || typeof d.type !== "string") return false;
  const handler = HUB_SHELL_HANDLERS[d.type];
  if (!handler) {
    console.warn(`[shell-ws] unhandled message type: ${d.type}`);
    return false;
  }
  handler(d);
  return true;
}
