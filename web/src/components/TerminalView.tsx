import { useEffect, useRef } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { useStore } from "../store.js";
import "@xterm/xterm/css/xterm.css";

interface Props {
  sessionId: string;
  visible?: boolean;
}

// Persist terminal instances across React re-mounts (StrictMode, tab switching)
const terminalInstances = new Map<
  string,
  { term: Terminal; fit: FitAddon; ws: WebSocket }
>();


function getOrCreateTerminal(sessionId: string) {
  const existing = terminalInstances.get(sessionId);
  if (existing) return existing;

  const term = new Terminal({
    cursorBlink: true,
    fontSize: 14,
    fontFamily:
      "ui-monospace, 'Cascadia Code', 'JetBrains Mono', Menlo, monospace",
    scrollback: 10000,
    theme: {
      background: "#1e1e2e",
      foreground: "#cdd6f4",
      cursor: "#f5e0dc",
      selectionBackground: "#585b7066",
    },
  });

  // Intercept Ctrl/Cmd+Alt shortcuts (events don't bubble out of xterm)
  term.attachCustomKeyEventHandler((e: KeyboardEvent) => {
    if (e.type === "keydown" && e.key === "k" && (e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey) {
      e.preventDefault();
      useStore.getState().toggleCommandPalette();
      return false;
    }
    if (e.type === "keydown" && e.altKey && (e.metaKey || e.ctrlKey)) {
      if (e.key === "s") {
        e.preventDefault();
        const s = useStore.getState();
        s.setSidebarOpen(!s.sidebarOpen);
        return false;
      }
      if (e.key === "p") {
        e.preventDefault();
        useStore.getState().toggleCommandPalette();
        return false;
      }
    }
    return true;
  });

  const fit = new FitAddon();
  term.loadAddon(fit);

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(
    `${proto}//${location.host}/ws/terminal/${sessionId}`,
  );
  ws.binaryType = "arraybuffer";

  let keepaliveInterval: ReturnType<typeof setInterval> | null = null;

  ws.onopen = () => {
    ws.send(
      JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }),
    );
    // Send application-level keepalive every 15s to prevent proxy timeouts
    keepaliveInterval = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "ping" }));
      }
    }, 15000);
  };

  ws.onmessage = (event) => {
    if (event.data instanceof ArrayBuffer) {
      term.write(new Uint8Array(event.data));
    } else if (typeof event.data === "string") {
      try {
        const ctrl = JSON.parse(event.data);
        if (ctrl.type === "exit") {
          term.writeln(
            `\r\n\x1b[90m[shell exited with code ${ctrl.code}]\x1b[0m`,
          );
        }
      } catch {
        // ignore
      }
    }
  };

  ws.onerror = (event) => {
    console.error("[terminal] WebSocket error for", sessionId, event);
    term.writeln("\r\n\x1b[31m[connection error]\x1b[0m");
  };

  ws.onclose = (event) => {
    console.warn("[terminal] WebSocket closed for", sessionId, "code:", event.code, "reason:", event.reason);
    if (keepaliveInterval) { clearInterval(keepaliveInterval); keepaliveInterval = null; }
    term.writeln("\r\n\x1b[90m[disconnected]\x1b[0m");
    terminalInstances.delete(sessionId);
  };

  term.onData((data) => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(new TextEncoder().encode(data));
    }
  });

  const instance = { term, fit, ws };
  terminalInstances.set(sessionId, instance);
  return instance;
}

export function destroyTerminal(sessionId: string) {
  const instance = terminalInstances.get(sessionId);
  if (instance) {
    instance.ws.close();
    instance.term.dispose();
    terminalInstances.delete(sessionId);
  }
}

// CSS to fully hide scrollbar and reclaim space when alt buffer is active.
// xterm.js has a known issue (#3074) where hiding the scrollbar via CSS
// leaves a gap because the viewport width calculation uses a 15px fallback.
// We override .xterm-viewport and .xterm-screen to fill the full width.
// --- Mobile touch support ---
// xterm.js has no native touch handling. We wire up gestures manually.
function attachTouchHandlers(term: Terminal, container: HTMLElement) {
  let startY = 0;
  let startX = 0;
  let scrollRemainder = 0;
  let moved = false;
  let longPressTimer: ReturnType<typeof setTimeout> | null = null;
  let lastTapTime = 0;
  let lastTapX = 0;
  let lastTapY = 0;
  let toolbar: HTMLDivElement | null = null;
  let toolbarTimer: ReturnType<typeof setTimeout> | null = null;

  function lineHeight() {
    return container.clientHeight / term.rows;
  }

  function tapToCell(x: number, y: number) {
    const rect = container.getBoundingClientRect();
    const col = Math.floor(((x - rect.left) / rect.width) * term.cols);
    const row = Math.floor(((y - rect.top) / rect.height) * term.rows);
    return { col: Math.max(0, Math.min(col, term.cols - 1)), row: Math.max(0, Math.min(row, term.rows - 1)) };
  }

  function findWordAt(col: number, bufferRow: number) {
    const line = term.buffer.active.getLine(bufferRow);
    if (!line) return null;
    const text = line.translateToString();
    if (col >= text.length) return null;
    const wordRe = /\w/;
    if (!wordRe.test(text[col])) return null;
    let start = col;
    while (start > 0 && wordRe.test(text[start - 1])) start--;
    let end = col;
    while (end < text.length - 1 && wordRe.test(text[end + 1])) end++;
    return { start, length: end - start + 1 };
  }

  function dismissToolbar() {
    if (toolbarTimer) { clearTimeout(toolbarTimer); toolbarTimer = null; }
    if (toolbar) { toolbar.remove(); toolbar = null; }
  }

  function showToolbar(x: number, y: number) {
    dismissToolbar();
    const tb = document.createElement("div");
    tb.style.cssText =
      "position:absolute;z-index:100;display:flex;gap:2px;padding:4px 6px;" +
      "background:rgba(0,0,0,0.85);border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,0.4);";
    const rect = container.getBoundingClientRect();
    tb.style.left = `${Math.max(4, Math.min(x - rect.left - 60, rect.width - 130))}px`;
    tb.style.top = `${Math.max(4, y - rect.top - 40)}px`;

    function btn(label: string, action: () => void) {
      const b = document.createElement("button");
      b.textContent = label;
      b.style.cssText =
        "color:#fff;background:none;border:none;font-size:12px;padding:4px 8px;" +
        "border-radius:4px;cursor:pointer;font-family:inherit;";
      b.addEventListener("pointerenter", () => { b.style.background = "rgba(255,255,255,0.15)"; });
      b.addEventListener("pointerleave", () => { b.style.background = "none"; });
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        action();
        dismissToolbar();
        term.focus();
      });
      return b;
    }

    tb.appendChild(btn("Copy", () => {
      const sel = term.getSelection();
      if (sel) navigator.clipboard.writeText(sel).catch(() => {});
    }));
    tb.appendChild(btn("Paste", () => {
      navigator.clipboard.readText().then((t) => { if (t) term.paste(t); }).catch(() => {});
    }));
    tb.appendChild(btn("Select All", () => {
      term.selectAll();
    }));

    container.style.position = "relative";
    container.appendChild(tb);
    toolbar = tb;
    toolbarTimer = setTimeout(dismissToolbar, 4000);
  }

  function onTouchStart(e: TouchEvent) {
    if (e.touches.length !== 1) return;
    const t = e.touches[0];
    startY = t.clientY;
    startX = t.clientX;
    scrollRemainder = 0;
    moved = false;

    // Long-press timer
    if (longPressTimer) clearTimeout(longPressTimer);
    longPressTimer = setTimeout(() => {
      longPressTimer = null;
      if (!moved) {
        navigator.clipboard.readText()
          .then((text) => { if (text) term.paste(text); term.focus(); })
          .catch(() => {});
      }
    }, 500);
  }

  function onTouchMove(e: TouchEvent) {
    if (e.touches.length !== 1) return;
    const t = e.touches[0];
    const dy = startY - t.clientY;
    const dx = t.clientX - startX;

    if (!moved && (Math.abs(dy) > 10 || Math.abs(dx) > 10)) {
      moved = true;
      dismissToolbar();
      if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
    }

    if (moved && Math.abs(dy) > Math.abs(dx)) {
      // Vertical scroll
      const lh = lineHeight();
      const raw = dy / lh + scrollRemainder;
      const lines = Math.trunc(raw);
      scrollRemainder = raw - lines;
      if (lines !== 0) {
        term.scrollLines(lines);
        startY = t.clientY;
        scrollRemainder = 0;
      }
      e.preventDefault();
    }
  }

  function onTouchEnd(e: TouchEvent) {
    if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }

    if (!moved && e.changedTouches.length === 1) {
      const t = e.changedTouches[0];
      const now = Date.now();
      const dist = Math.hypot(t.clientX - lastTapX, t.clientY - lastTapY);

      if (now - lastTapTime < 300 && dist < 20) {
        // Double-tap → select word
        const { col, row } = tapToCell(t.clientX, t.clientY);
        const bufRow = term.buffer.active.viewportY + row;
        const word = findWordAt(col, bufRow);
        if (word) {
          term.select(word.start, bufRow, word.length);
          showToolbar(t.clientX, t.clientY);
        }
        lastTapTime = 0; // reset so triple-tap doesn't fire
      } else {
        // Single tap → show toolbar (after short delay to allow double-tap detection)
        lastTapTime = now;
        lastTapX = t.clientX;
        lastTapY = t.clientY;
        const tapX = t.clientX, tapY = t.clientY;
        setTimeout(() => {
          if (lastTapTime === now) {
            // No second tap arrived — it's a single tap
            showToolbar(tapX, tapY);
          }
        }, 310);
      }
    }
  }

  const el = term.element;
  if (!el) return () => {};

  el.addEventListener("touchstart", onTouchStart, { passive: true });
  el.addEventListener("touchmove", onTouchMove, { passive: false });
  el.addEventListener("touchend", onTouchEnd, { passive: true });

  return () => {
    el.removeEventListener("touchstart", onTouchStart);
    el.removeEventListener("touchmove", onTouchMove);
    el.removeEventListener("touchend", onTouchEnd);
    dismissToolbar();
    if (longPressTimer) clearTimeout(longPressTimer);
  };
}

const SCROLLBAR_CSS = `
.terminal-no-scrollbar .xterm-viewport {
  overflow-y: hidden !important;
  scrollbar-width: none !important;
}
.terminal-no-scrollbar .xterm-viewport::-webkit-scrollbar {
  display: none !important;
}
.terminal-no-scrollbar .xterm-screen {
  width: 100% !important;
}
`;

export function TerminalView({ sessionId, visible }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);

  // Focus terminal when it becomes visible (tab switch or initial creation)
  useEffect(() => {
    if (!visible) return;
    const instance = terminalInstances.get(sessionId);
    if (instance) {
      requestAnimationFrame(() => instance.term.focus());
    }
  }, [visible, sessionId]);

  // Focus terminal when pendingFocus signals "terminal"
  const pendingFocus = useStore((s) => s.pendingFocus);
  const currentSessionId = useStore((s) => s.currentSessionId);
  useEffect(() => {
    if (pendingFocus === "terminal" && visible && currentSessionId === sessionId) {
      const instance = terminalInstances.get(sessionId);
      if (instance) {
        instance.term.focus();
      }
      useStore.getState().setPendingFocus(null);
    }
  }, [pendingFocus, visible, currentSessionId, sessionId]);

  useEffect(() => {
    if (!containerRef.current) return;

    const { term, fit, ws } = getOrCreateTerminal(sessionId);

    // Attach to DOM (re-open if previously detached)
    if (!term.element) {
      term.open(containerRef.current);
    } else {
      containerRef.current.appendChild(term.element);
    }

    // Focus on initial mount
    requestAnimationFrame(() => term.focus());

    // Hide scrollbar when alternate screen buffer is active (tmux, vim, etc.)
    function updateScrollbar() {
      const altBuffer = term.buffer.active.type === "alternate";
      containerRef.current?.classList.toggle("terminal-no-scrollbar", altBuffer);
      // Re-fit after a frame so layout recalculates with new scrollbar state
      requestAnimationFrame(() => {
        fit.fit();
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
        }
      });
    }
    const bufferDisposable = term.buffer.onBufferChange(updateScrollbar);
    // Apply initial state (e.g. reconnecting to already-running tmux)
    updateScrollbar();

    // Resize handling
    const resizeObserver = new ResizeObserver(() => {
      fit.fit();
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(
          JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }),
        );
      }
    });
    resizeObserver.observe(containerRef.current);

    // Mobile touch gestures (scroll, select, paste, toolbar)
    const detachTouch = attachTouchHandlers(term, containerRef.current);

    return () => {
      resizeObserver.disconnect();
      bufferDisposable.dispose();
      detachTouch();
    };
  }, [sessionId]);

  return (
    <>
      <style>{SCROLLBAR_CSS}</style>
      <div ref={containerRef} className="w-full h-full" />
    </>
  );
}
