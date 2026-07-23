// Node mode (contract ui/v1, docs/hub-node-contract-v1.md):
// this build is being served by a vibr8 node through the hub's
// /nodes/{id}/ui/ proxy. All API and WebSocket traffic must stay inside
// the /nodes/{id}/ prefix so it reaches the vending node, not the hub.
// Outside the proxy (hub root, dev server) the prefix is empty and
// nothing changes.

const _path: string =
  (typeof window !== "undefined" && window.location?.pathname) || "";
const m = _path.match(/^\/nodes\/([^/]+)\/ui(\/|$)/);

export const NODE_MODE: boolean = m !== null;
export const NODE_ID: string | null = m ? m[1] : null;
export const BASE_PREFIX: string = m ? `/nodes/${m[1]}` : "";
