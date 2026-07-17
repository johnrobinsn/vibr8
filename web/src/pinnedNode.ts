// Deeplink: `/@<node-name-or-id>` locks this browser tab to one
// node. The node switcher in the shell strip is replaced with a
// static name, voice/Ring0 stay wired to that node, and if the
// pinned node isn't registered/online the shell renders a
// pin-specific unavailable card instead of silently falling back to
// a different node.
//
// Sharing the URL sends the recipient to the same pinned node. The
// pin is per-tab (URL lives in that tab), so the user's other tabs
// are unaffected.
//
// Supported forms (in order of preference — first match wins):
//   /@<name>            canonical, prettiest
//   /n/<name>           equivalent, useful for scripts / composability
//   /?pin=<name>        legacy query form, still honored so old
//                       bookmarks and links keep working
//
// The prod hub's SPA catchall (server/main.py:899) already serves
// the shell HTML for any non-file path, and Vite's default SPA
// mode does the same in dev — so no server routing changes are
// needed to make the path forms load the shell.
//
// Ignored in NODE_MODE — the vended iframe is already node-scoped,
// so an inherited pin there would be redundant at best and
// confusing at worst.

import { NODE_MODE } from "./nodeMode.js";

// Path segments the shell reserves for its own use — anything here
// must not be interpreted as a pin (e.g. an accidental URL like
// `/@settings` would otherwise pin to a nonexistent node named
// "settings"). Kept intentionally tiny; only reserved prefixes we
// actually use.
const RESERVED_TOP_PATHS = new Set(["", "assets", "api", "ws", "nodes"]);

function _pathPin(pathname: string): string | null {
  // Strip leading slashes and split off the first segment only —
  // node names shouldn't contain "/" but we bound the parse either
  // way so trailing path fragments don't leak in.
  const trimmed = pathname.replace(/^\/+/, "");
  const firstSeg = trimmed.split("/", 1)[0] ?? "";

  let raw: string | null = null;
  if (firstSeg.startsWith("@")) {
    raw = firstSeg.slice(1);
  } else if (firstSeg === "n") {
    // `/n/<name>` — take the second segment.
    const parts = trimmed.split("/");
    raw = parts[1] ?? "";
  } else {
    return null;
  }

  if (!raw || RESERVED_TOP_PATHS.has(raw)) return null;
  try {
    return decodeURIComponent(raw).trim() || null;
  } catch {
    return raw.trim() || null;
  }
}

function _queryPin(search: string): string | null {
  try {
    const params = new URLSearchParams(search);
    const v = (params.get("pin") ?? "").trim();
    return v || null;
  } catch {
    return null;
  }
}

function _readPin(): string | null {
  if (typeof window === "undefined") return null;
  if (NODE_MODE) return null;
  return (
    _pathPin(window.location.pathname) ?? _queryPin(window.location.search)
  );
}

/** Raw pin string from the URL, or null when unpinned. Read once at
 * module load — the URL cannot change without a full page reload. */
export const PINNED_NODE: string | null = _readPin();

// Exposed for tests — the module-level PINNED_NODE constant is
// captured at load time, so test coverage of the URL parsing needs a
// pure function it can drive with synthetic locations.
export const _parsePin = {
  fromPath: _pathPin,
  fromSearch: _queryPin,
};

/** Resolve a pin string against a node list.
 *
 * Match order:
 *   1. Exact name, case-insensitive.
 *   2. Node id starting with the pin string.
 *
 * Returns null if no node matches. Prefer name matching because the
 * pin is human-typed in a URL. */
export function resolvePinnedNode<T extends { id: string; name: string }>(
  pin: string,
  nodes: readonly T[],
): T | null {
  if (!pin) return null;
  const lower = pin.toLowerCase();
  const byName = nodes.find((n) => n.name.toLowerCase() === lower);
  if (byName) return byName;
  const byIdPrefix = nodes.find((n) => n.id.startsWith(pin));
  return byIdPrefix ?? null;
}
