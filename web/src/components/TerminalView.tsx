import { useEffect, useRef } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
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

  const fit = new FitAddon();
  term.loadAddon(fit);

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(
    `${proto}//${location.host}/ws/terminal/${sessionId}`,
  );
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    ws.send(
      JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }),
    );
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

  ws.onclose = () => {
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

    return () => {
      resizeObserver.disconnect();
      bufferDisposable.dispose();
    };
  }, [sessionId]);

  return (
    <>
      <style>{SCROLLBAR_CSS}</style>
      <div ref={containerRef} className="w-full h-full" />
    </>
  );
}
