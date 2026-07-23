// True when the page is served by dev-launch.sh — locally by port
// (dev hub 4456, dev Vite 5184) or externally by hostname prefix
// (dev-vibr8.ringzero.ai). Used to overlay a red dot on the shell
// logo + favicon so dev tabs are visually distinct from prod tabs.
export function isDevInstance(): boolean {
  if (typeof window === "undefined") return false;
  const { port, hostname } = window.location;
  if (port === "5184" || port === "4456") return true;
  if (hostname.startsWith("dev-") || hostname.startsWith("dev.")) return true;
  return false;
}
