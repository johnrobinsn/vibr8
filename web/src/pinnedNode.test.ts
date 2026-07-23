// @vitest-environment jsdom
import { describe, it, expect } from "vitest";

import { resolvePinnedNode, _parsePin } from "./pinnedNode.js";

type Node = { id: string; name: string };

const NODES: Node[] = [
  { id: "5b724a910a9e27ba49ff78ca53aeee83", name: "blah" },
  { id: "d40201bc00000000000000000000000a", name: "hello" },
  { id: "5b7ffffffffffffffffffffffffffffe", name: "second" }, // deliberate id-prefix collision hint
];

describe("resolvePinnedNode", () => {
  it("matches by exact name, case-insensitive", () => {
    expect(resolvePinnedNode("blah", NODES)?.id).toBe(NODES[0].id);
    expect(resolvePinnedNode("Blah", NODES)?.id).toBe(NODES[0].id);
    expect(resolvePinnedNode("BLAH", NODES)?.id).toBe(NODES[0].id);
  });

  it("prefers name over id-prefix when both would match", () => {
    // Suppose someone names a node "5b7" — the name match wins over
    // the id-prefix match against the sibling node.
    const withNameCollision: Node[] = [
      { id: "aaaaaaaa1", name: "5b7" },
      ...NODES,
    ];
    expect(resolvePinnedNode("5b7", withNameCollision)?.name).toBe("5b7");
  });

  it("matches by id prefix when name doesn't match", () => {
    expect(resolvePinnedNode("5b724a91", NODES)?.name).toBe("blah");
  });

  it("returns null when nothing matches", () => {
    expect(resolvePinnedNode("nonesuch", NODES)).toBeNull();
    expect(resolvePinnedNode("", NODES)).toBeNull();
  });

  it("handles empty node list", () => {
    expect(resolvePinnedNode("blah", [])).toBeNull();
  });
});

describe("_parsePin.fromPath", () => {
  it("recognizes /@<name> as the canonical pretty form", () => {
    expect(_parsePin.fromPath("/@blah")).toBe("blah");
    expect(_parsePin.fromPath("/@hermes")).toBe("hermes");
  });

  it("recognizes /n/<name> as a synonym", () => {
    expect(_parsePin.fromPath("/n/blah")).toBe("blah");
  });

  it("takes only the first path segment — trailing junk doesn't leak", () => {
    // The shell doesn't route sub-paths under a pin today; anything
    // after the name is dropped so the pin string stays clean.
    expect(_parsePin.fromPath("/@blah/whatever")).toBe("blah");
    expect(_parsePin.fromPath("/n/blah/nested")).toBe("blah");
  });

  it("URL-decodes names with escaped characters", () => {
    expect(_parsePin.fromPath("/@node%20name")).toBe("node name");
    expect(_parsePin.fromPath("/@%40weird")).toBe("@weird");
  });

  it("returns null for unrecognized shapes", () => {
    expect(_parsePin.fromPath("/")).toBeNull();
    expect(_parsePin.fromPath("/nodes/abc/ui/")).toBeNull();
    expect(_parsePin.fromPath("/settings")).toBeNull();
    expect(_parsePin.fromPath("/api/foo")).toBeNull();
  });

  it("rejects reserved names in the /@ position — protects future top-level routes", () => {
    // A path like `/@api` shouldn't get interpreted as pinning to a
    // node named "api". Same for other reserved prefixes.
    expect(_parsePin.fromPath("/@api")).toBeNull();
    expect(_parsePin.fromPath("/@nodes")).toBeNull();
    expect(_parsePin.fromPath("/@assets")).toBeNull();
  });

  it("returns null for an empty @ or n/ segment", () => {
    expect(_parsePin.fromPath("/@")).toBeNull();
    expect(_parsePin.fromPath("/n/")).toBeNull();
  });
});

describe("_parsePin.fromSearch — legacy ?pin= query form", () => {
  it("still works so old bookmarks keep resolving", () => {
    expect(_parsePin.fromSearch("?pin=blah")).toBe("blah");
    expect(_parsePin.fromSearch("?pin=hermes&other=x")).toBe("hermes");
  });

  it("returns null when pin is absent or empty", () => {
    expect(_parsePin.fromSearch("")).toBeNull();
    expect(_parsePin.fromSearch("?other=x")).toBeNull();
    expect(_parsePin.fromSearch("?pin=")).toBeNull();
  });
});
