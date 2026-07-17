// @vitest-environment jsdom
import { vi, describe, it, expect, beforeEach } from "vitest";

// jsdom does not implement window.matchMedia — stub it before store.ts imports.
vi.hoisted(() => {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
});

// Names utility gets pulled in by store; stub it the same way ws.test.ts does.
vi.mock("./utils/names.js", () => ({
  generateUniqueSessionName: vi.fn(() => "Test Session"),
}));

let router: typeof import("./hubShellRouter.js");
let store: typeof import("./store.js");
let ws: typeof import("./ws.js");

beforeEach(async () => {
  vi.resetModules();
  router = await import("./hubShellRouter.js");
  store = await import("./store.js");
  ws = await import("./ws.js");
  // Reset store to a clean baseline for each test.
  store.useStore.setState({
    voiceMode: null,
    nodes: [],
    activeNodeId: "",
  } as any);
});

describe("dispatchHubShellMessage", () => {
  it("routes voice_mode → setVoiceMode", () => {
    const ok = router.dispatchHubShellMessage(
      JSON.stringify({ type: "voice_mode", mode: "active" }),
    );
    expect(ok).toBe(true);
    expect(store.useStore.getState().voiceMode).toBe("active");
  });

  it("routes node_title → setNodeTitle for the named node", () => {
    // Seed a node so setNodeTitle has something to attach to.
    store.useStore.setState({
      nodes: [{ id: "node-a", name: "A", status: "online", contract: [] }],
    } as any);
    const ok = router.dispatchHubShellMessage(
      JSON.stringify({ type: "node_title", nodeId: "node-a", text: "current-session" }),
    );
    expect(ok).toBe(true);
    const nodes = store.useStore.getState().nodes;
    expect(nodes.find((n) => n.id === "node-a")?.title).toBe("current-session");
  });

  it("routes ring0_switch_node via applyLocalNodeSwitch when the tab is unpinned", () => {
    // applyLocalNodeSwitch bails on empty / no-change, so we set an
    // initial node id different from the target.
    store.useStore.setState({
      nodes: [
        { id: "node-a", name: "A", status: "online", contract: [] },
        { id: "node-b", name: "B", status: "online", contract: [] },
      ],
      activeNodeId: "node-a",
    } as any);
    const spy = vi.spyOn(ws, "applyLocalNodeSwitch");
    const ok = router.dispatchHubShellMessage(
      JSON.stringify({ type: "ring0_switch_node", nodeId: "node-b" }),
    );
    expect(ok).toBe(true);
    expect(spy).toHaveBeenCalledWith("node-b");
    spy.mockRestore();
  });

  it("drops ring0_switch_node when the tab is pinned — client-side belt to the server-side denial", async () => {
    // Simulate a deeplinked shell: PINNED_NODE is truthy. Because the
    // module-level constant is read at import time, we reset modules,
    // mock ./pinnedNode.js to return a fixed value, and re-import the
    // router so it picks up the mocked module.
    vi.resetModules();
    vi.doMock("./pinnedNode.js", async (importOriginal) => {
      const actual = await importOriginal<typeof import("./pinnedNode.js")>();
      return { ...actual, PINNED_NODE: "blah" };
    });
    const pinnedRouter = await import("./hubShellRouter.js");
    const pinnedWs = await import("./ws.js");
    const spy = vi.spyOn(pinnedWs, "applyLocalNodeSwitch");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    const ok = pinnedRouter.dispatchHubShellMessage(
      JSON.stringify({ type: "ring0_switch_node", nodeId: "other-node" }),
    );
    // Router still returns true — the handler ran and made a
    // deliberate choice to drop the message.
    expect(ok).toBe(true);
    expect(spy).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining("ignored — tab is pinned to 'blah'"),
    );
    spy.mockRestore();
    warn.mockRestore();
    vi.doUnmock("./pinnedNode.js");
  });

  it("warns and returns false on an unknown type — the visible signal that a new server push type needs a shell handler", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const ok = router.dispatchHubShellMessage(
      JSON.stringify({ type: "something_new", payload: 42 }),
    );
    expect(ok).toBe(false);
    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining("[shell-ws] unhandled message type: something_new"),
    );
    warn.mockRestore();
  });

  it("ignores malformed JSON without throwing", () => {
    const ok = router.dispatchHubShellMessage("not json");
    expect(ok).toBe(false);
  });

  it("ignores non-object payloads", () => {
    const ok = router.dispatchHubShellMessage("null");
    expect(ok).toBe(false);
  });

  it("does NOT register ring0_switch_ui — session-scoped pushes are node→iframe, not through the shell", () => {
    // Codifies the design principle: adding ring0_switch_ui to the
    // handler table here would be a design regression (see
    // docs/hub-node-contract-v1.md "Design principles"). If some
    // future change legitimately needs the shell to observe session
    // switches, that decision belongs in a doc amendment first.
    expect(router.HUB_SHELL_HANDLERS).not.toHaveProperty("ring0_switch_ui");
  });
});
