// Rendered when the URL carries ?pin=<node> but that node isn't
// currently a usable ui/v1 target — either it's not registered with
// this hub at all, or it's registered but offline. The shell keeps
// the same header layout (logo + node label) so the user isn't
// disoriented; the content area explains what's wrong so they know
// whether to bring the node online or fix the URL.

export function PinnedNodeUnavailable({
  pin,
  reason,
}: {
  pin: string;
  reason: "offline" | "unregistered";
}) {
  const title = reason === "offline" ? "Node offline" : "Node not registered";
  const detail =
    reason === "offline"
      ? "The node this URL is pinned to is registered but not currently online."
      : "No node with this name or id is registered with this hub.";

  return (
    <div className="h-[100dvh] flex flex-col bg-cc-bg text-cc-fg">
      {/* Header — matches NodeShellFrame's strip layout so the pinned
          identity stays visible even when the target is unavailable. */}
      <div className="flex items-center gap-2 px-3 py-1.5 border-b border-cc-border shrink-0">
        <div className="flex items-center gap-1.5 mr-1">
          <img src="/logo.svg" alt="" className="h-[1.2em] w-auto" />
          <span className="text-sm font-semibold text-cc-fg tracking-tight">
            vibr8
          </span>
        </div>
        <span className="text-xs text-cc-muted">node</span>
        <span
          className="px-2 py-1 text-xs text-cc-fg font-medium"
          title="This URL is pinned to a single node"
        >
          {pin}
        </span>
      </div>

      <div className="flex-1 flex items-center justify-center text-cc-muted p-6">
        <div className="text-center space-y-3 max-w-md">
          <svg
            viewBox="0 0 16 16"
            fill="none"
            stroke="currentColor"
            strokeWidth="1"
            className="w-12 h-12 mx-auto opacity-30"
            aria-hidden
          >
            <rect x="2" y="3" width="12" height="8" rx="1" />
            <path d="M5 14h6M8 11v3" />
          </svg>
          <p className="text-cc-fg font-medium">{title}</p>
          <p className="text-xs opacity-70 leading-relaxed">
            {detail} This URL is pinned to{" "}
            <span className="font-mono text-cc-fg">{pin}</span>.
          </p>
          <p className="text-xs opacity-60">
            The page updates automatically once the node comes online.
          </p>
        </div>
      </div>
    </div>
  );
}
