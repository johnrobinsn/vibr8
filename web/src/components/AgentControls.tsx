/**
 * Agent-specific controls for computer-use sessions.
 * Shows Watch/Act mode toggle, execution mode selector, and confirm dialog.
 * Only rendered when the current session is a computer-use session.
 */
import { useStore } from "../store.js";
import { sendToSession } from "../ws.js";

export function AgentControls({ sessionId }: { sessionId: string }) {
  const agentMode = useStore((s) => s.agentMode);
  const executionMode = useStore((s) => s.executionMode);
  const pendingConfirmation = useStore((s) => s.pendingConfirmation);
  const setAgentMode = useStore((s) => s.setAgentMode);
  const setExecutionMode = useStore((s) => s.setExecutionMode);

  const switchToWatch = () => {
    setAgentMode("watch");
    sendToSession(sessionId, { type: "watch_start" });
  };

  const switchToAct = () => {
    setAgentMode("act");
    sendToSession(sessionId, { type: "watch_stop" });
  };

  const handleApprove = () => {
    sendToSession(sessionId, { type: "approve" });
  };

  const handleReject = () => {
    sendToSession(sessionId, { type: "reject" });
  };

  return (
    <div className="flex flex-col gap-2">
      {/* Mode toggle + execution mode */}
      <div className="flex items-center gap-2 px-3 py-1.5 text-xs">
        {/* Watch / Act toggle */}
        <div className="flex rounded-md border border-cc-border overflow-hidden">
          <button
            onClick={switchToWatch}
            className={`px-2.5 py-1 transition-colors ${
              agentMode === "watch"
                ? "bg-orange-500/20 text-orange-600 dark:text-orange-400"
                : "text-cc-text-secondary hover:bg-cc-hover"
            }`}
          >
            Watch
          </button>
          <button
            onClick={switchToAct}
            className={`px-2.5 py-1 transition-colors ${
              agentMode === "act"
                ? "bg-orange-500/20 text-orange-600 dark:text-orange-400"
                : "text-cc-text-secondary hover:bg-cc-hover"
            }`}
          >
            Act
          </button>
        </div>

        {/* Execution mode (only in Act mode) */}
        {agentMode === "act" && (
          <select
            value={executionMode}
            onChange={(e) => setExecutionMode(e.target.value as "auto" | "confirm" | "gated")}
            className="text-xs bg-cc-bg border border-cc-border rounded px-1.5 py-1 text-cc-text"
          >
            <option value="auto">Auto</option>
            <option value="confirm">Confirm</option>
            <option value="gated">Gated</option>
          </select>
        )}
      </div>

      {/* Confirm dialog */}
      {pendingConfirmation && (
        <div className="mx-3 mb-2 p-3 rounded-lg border border-orange-500/30 bg-orange-500/5">
          <div className="text-xs text-cc-text-secondary mb-1">Step {pendingConfirmation.step} — Confirm action</div>
          {pendingConfirmation.thought && (
            <div className="text-sm text-cc-text mb-1">{pendingConfirmation.thought}</div>
          )}
          <div className="text-sm font-mono text-orange-600 dark:text-orange-400 mb-2">
            {pendingConfirmation.actionSummary}
          </div>
          <div className="flex gap-2">
            <button
              onClick={handleApprove}
              className="px-3 py-1 text-xs rounded bg-green-600 text-white hover:bg-green-700 transition-colors"
            >
              Approve
            </button>
            <button
              onClick={handleReject}
              className="px-3 py-1 text-xs rounded bg-red-600/20 text-red-600 dark:text-red-400 hover:bg-red-600/30 transition-colors"
            >
              Reject
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
