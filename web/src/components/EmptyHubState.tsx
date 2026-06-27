// Shown when no online vending node is registered with the hub. This is
// the bootstrap entry point: there is no "self" node anymore — every
// node, including the host, is a separate vibr8_node process the
// operator runs and registers.

export function EmptyHubState() {
  return (
    <div className="h-[100dvh] flex items-center justify-center bg-cc-bg text-cc-fg p-6 font-sans-ui">
      <div className="max-w-xl w-full space-y-5">
        <div>
          <h1 className="text-2xl font-medium mb-1">No nodes connected</h1>
          <p className="text-cc-muted text-sm">
            The hub is a stateless router. Sessions, Ring0, files, and
            agents live on <em>nodes</em>. Register and start one to begin.
          </p>
        </div>

        <div className="rounded-lg border border-cc-border bg-cc-panel/30 p-4 text-sm space-y-3">
          <div>
            <div className="text-cc-muted text-xs uppercase tracking-wide mb-1">
              1. Issue an API key from Settings → Nodes
            </div>
            <a
              href="#/settings/nodes"
              className="inline-block text-cc-primary hover:underline"
            >
              Open node settings →
            </a>
          </div>

          <div>
            <div className="text-cc-muted text-xs uppercase tracking-wide mb-1">
              2. Start a node on the machine you want to run agents on
            </div>
            <pre className="bg-cc-bg border border-cc-border rounded p-3 overflow-x-auto text-xs leading-relaxed">
              {`uv run --from git+https://github.com/johnrobinsn/vibr8 \\
  python -m vibr8_node \\
  --hub wss://${typeof location !== "undefined" ? location.host : "your-hub"} \\
  --api-key <paste from step 1> \\
  --name $(hostname)`}
            </pre>
          </div>
        </div>

        <p className="text-cc-muted text-xs">
          The page reloads automatically once a node registers.
        </p>
      </div>
    </div>
  );
}
