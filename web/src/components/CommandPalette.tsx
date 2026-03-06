import { useState, useEffect, useRef, useMemo, useCallback } from "react";
import { createPortal } from "react-dom";
import { useStore } from "../store.js";
import { commands, type Command, type CommandResult } from "../commandRegistry.js";
import { connectSession, disconnectSession } from "../ws.js";

interface PaletteItem {
  type: "session" | "command";
  id: string;
  label: string;
  secondary?: string;
  backendType?: string;
  isConnected?: boolean;
  isCurrent?: boolean;
  icon?: "session" | "ui";
}

export function CommandPalette() {
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [inputMode, setInputMode] = useState<{ command: Command; prompt: string } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const close = useStore((s) => s.setCommandPaletteOpen);

  const sdkSessions = useStore((s) => s.sdkSessions);
  const sessionNames = useStore((s) => s.sessionNames);
  const currentSessionId = useStore((s) => s.currentSessionId);
  const cliConnected = useStore((s) => s.cliConnected);

  // Auto-focus input
  useEffect(() => {
    requestAnimationFrame(() => inputRef.current?.focus());
  }, []);

  // Build items list
  const items = useMemo<PaletteItem[]>(() => {
    if (inputMode) return [];

    const q = query.toLowerCase();
    const result: PaletteItem[] = [];

    // Sessions (active only)
    const activeSessions = sdkSessions
      .filter((s) => !s.archived)
      .sort((a, b) => (b.createdAt ?? 0) - (a.createdAt ?? 0));

    for (const s of activeSessions) {
      const name = sessionNames.get(s.sessionId) || s.sessionId.slice(0, 8);
      const dir = s.cwd ? s.cwd.split("/").pop() || "" : "";
      const searchText = `${name} ${dir} ${s.sessionId} ${s.backendType || ""}`.toLowerCase();
      if (q && !searchText.includes(q)) continue;
      result.push({
        type: "session",
        id: s.sessionId,
        label: name,
        secondary: dir,
        backendType: s.backendType,
        isConnected: cliConnected.get(s.sessionId) ?? false,
        isCurrent: s.sessionId === currentSessionId,
      });
    }

    // Commands
    for (const cmd of commands) {
      const searchText = `${cmd.id} ${cmd.label}`.toLowerCase();
      if (q && !searchText.includes(q)) continue;
      result.push({
        type: "command",
        id: cmd.id,
        label: cmd.label,
        secondary: cmd.id,
        icon: cmd.icon,
      });
    }

    return result;
  }, [query, inputMode, sdkSessions, sessionNames, currentSessionId, cliConnected]);

  // Keep selected index in bounds
  useEffect(() => {
    if (selectedIndex >= items.length) {
      setSelectedIndex(Math.max(0, items.length - 1));
    }
  }, [items.length, selectedIndex]);

  // Scroll selected item into view
  useEffect(() => {
    if (!listRef.current) return;
    const el = listRef.current.querySelector(`[data-idx="${selectedIndex}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [selectedIndex]);

  const selectSession = useCallback((sessionId: string) => {
    const s = useStore.getState();
    const newSdk = s.sdkSessions.find((x) => x.sessionId === sessionId);
    const isTerminal = newSdk?.backendType === "terminal";
    if (s.currentSessionId !== sessionId) {
      if (s.currentSessionId) {
        const oldSdk = s.sdkSessions.find((x) => x.sessionId === s.currentSessionId);
        if (oldSdk?.backendType !== "terminal") {
          disconnectSession(s.currentSessionId);
        }
      }
      s.setCurrentSession(sessionId);
      if (!isTerminal) {
        connectSession(sessionId);
      }
    }
    s.setPendingFocus(isTerminal ? "terminal" : "composer");
    close(false);
  }, [close]);

  const executeCommand = useCallback(async (cmd: Command, param?: string) => {
    const result: CommandResult = await cmd.execute({ param });
    if (result && typeof result === "object" && "needsInput" in result) {
      setInputMode({ command: cmd, prompt: result.needsInput });
      setQuery("");
      requestAnimationFrame(() => inputRef.current?.focus());
      return;
    }
    close(false);
  }, [close]);

  const selectItem = useCallback((item: PaletteItem) => {
    if (item.type === "session") {
      selectSession(item.id);
    } else {
      const cmd = commands.find((c) => c.id === item.id);
      if (cmd) executeCommand(cmd);
    }
  }, [selectSession, executeCommand]);

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      e.preventDefault();
      if (inputMode) {
        setInputMode(null);
        setQuery("");
      } else {
        close(false);
      }
      return;
    }

    if (inputMode) {
      if (e.key === "Enter") {
        e.preventDefault();
        executeCommand(inputMode.command, query);
      }
      return;
    }

    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelectedIndex((i) => (items.length > 0 ? (i + 1) % items.length : 0));
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelectedIndex((i) => (items.length > 0 ? (i - 1 + items.length) % items.length : 0));
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      if (items[selectedIndex]) {
        selectItem(items[selectedIndex]);
      }
    }
  }

  // Find where sessions end and commands begin for group headers
  const firstCommandIdx = items.findIndex((i) => i.type === "command");
  const hasSessionItems = items.some((i) => i.type === "session");
  const hasCommandItems = firstCommandIdx !== -1;

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex justify-center pt-[15vh] sm:pt-[20vh] bg-black/50"
      onClick={() => close(false)}
    >
      <div
        className="w-full max-w-md h-fit bg-cc-bg border border-cc-border rounded-[14px] shadow-2xl overflow-hidden mx-4"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Search input */}
        <div className="flex items-center gap-2 px-4 py-3 border-b border-cc-border">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-4 h-4 text-cc-muted shrink-0">
            <circle cx="7" cy="7" r="4.5" />
            <path d="M10.5 10.5L14 14" strokeLinecap="round" />
          </svg>
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => { setQuery(e.target.value); setSelectedIndex(0); }}
            onKeyDown={handleKeyDown}
            placeholder={inputMode ? inputMode.prompt : "Search sessions and commands..."}
            className="flex-1 bg-transparent text-sm text-cc-fg placeholder:text-cc-muted outline-none"
            autoComplete="off"
            spellCheck={false}
          />
          {inputMode && (
            <span className="text-[10px] text-cc-muted bg-cc-hover px-2 py-0.5 rounded">
              {inputMode.command.label}
            </span>
          )}
        </div>

        {/* Results */}
        {!inputMode && (
          <div ref={listRef} className="max-h-[50vh] overflow-y-auto py-1">
            {items.length === 0 ? (
              <p className="px-4 py-6 text-sm text-cc-muted text-center">No matches</p>
            ) : (
              <>
                {hasSessionItems && (
                  <div className="px-3 pt-2 pb-1 text-[10px] uppercase tracking-wider text-cc-muted font-medium">
                    Sessions
                  </div>
                )}
                {items.map((item, i) => (
                  <div key={`${item.type}-${item.id}`}>
                    {i === firstCommandIdx && hasCommandItems && (
                      <div className="px-3 pt-3 pb-1 text-[10px] uppercase tracking-wider text-cc-muted font-medium">
                        Commands
                      </div>
                    )}
                    <button
                      data-idx={i}
                      onClick={() => selectItem(item)}
                      className={`w-full px-3 py-2.5 mx-1 text-left rounded-[10px] transition-colors cursor-pointer flex items-center gap-2.5 min-h-[44px] ${
                        i === selectedIndex ? "bg-cc-hover" : "hover:bg-cc-hover/50"
                      }`}
                      style={{ width: "calc(100% - 8px)" }}
                    >
                      {item.type === "session" ? (
                        <>
                          <span className={`w-2 h-2 rounded-full shrink-0 ${
                            item.isConnected ? "bg-cc-success" : "bg-cc-muted opacity-40"
                          }`} />
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-1.5">
                              <span className={`text-[13px] font-medium truncate ${item.isCurrent ? "text-cc-primary" : "text-cc-fg"}`}>
                                {item.label}
                              </span>
                              {item.backendType === "terminal" && (
                                <span className="text-[9px] px-1 py-0.5 rounded bg-green-500/15 text-green-600 dark:text-green-400 shrink-0">term</span>
                              )}
                              {item.backendType === "codex" && (
                                <span className="text-[9px] px-1 py-0.5 rounded bg-purple-500/15 text-purple-600 dark:text-purple-400 shrink-0">codex</span>
                              )}
                            </div>
                            {item.secondary && (
                              <span className="text-[11px] text-cc-muted truncate block">{item.secondary}</span>
                            )}
                          </div>
                          {item.isCurrent && (
                            <svg viewBox="0 0 16 16" fill="currentColor" className="w-3.5 h-3.5 text-cc-primary shrink-0">
                              <path d="M13.78 4.22a.75.75 0 010 1.06l-7.25 7.25a.75.75 0 01-1.06 0L2.22 9.28a.75.75 0 011.06-1.06L6 10.94l6.72-6.72a.75.75 0 011.06 0z" />
                            </svg>
                          )}
                        </>
                      ) : (
                        <>
                          <span className="flex items-center justify-center w-6 h-6 rounded-md bg-cc-hover text-cc-muted shrink-0">
                            {item.icon === "session" ? (
                              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-3.5 h-3.5">
                                <rect x="3" y="3" width="10" height="10" rx="2" />
                                <path d="M6 8h4" strokeLinecap="round" />
                              </svg>
                            ) : (
                              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" className="w-3.5 h-3.5">
                                <path d="M4 6l4 4 4-4" strokeLinecap="round" strokeLinejoin="round" />
                              </svg>
                            )}
                          </span>
                          <div className="flex-1 min-w-0">
                            <span className="text-[13px] font-medium text-cc-fg">{item.label}</span>
                            {item.secondary && (
                              <span className="ml-2 text-[11px] text-cc-muted">{item.secondary}</span>
                            )}
                          </div>
                        </>
                      )}
                    </button>
                  </div>
                ))}
              </>
            )}
          </div>
        )}

        {/* Footer hint */}
        <div className="px-3 py-2 border-t border-cc-border flex items-center gap-3 text-[10px] text-cc-muted">
          <span><kbd className="px-1 py-0.5 rounded bg-cc-hover text-cc-muted">↑↓</kbd> navigate</span>
          <span><kbd className="px-1 py-0.5 rounded bg-cc-hover text-cc-muted">↵</kbd> select</span>
          <span><kbd className="px-1 py-0.5 rounded bg-cc-hover text-cc-muted">esc</kbd> close</span>
        </div>
      </div>
    </div>,
    document.body,
  );
}
