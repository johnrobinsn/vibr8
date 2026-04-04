# Computer Use Agent — Architecture Questions

*From vibr8 planning session, 2026-04-01*

## Fundamental Architecture

**1. Native computer use tool vs MCP tool vs hybrid?**
Anthropic has a `computer_20250124` tool type the model is trained on. Ring0 only gets tools via MCP. Three options:
- **A**: Custom MCP tool that mimics the computer use schema. Works through Ring0's existing MCP pipeline. Claude can use it but lacks the specialized computer-use grounding training.
- **B**: Separate agent loop using the Anthropic API directly with the native `computer_20250124` tool type. Ring0 delegates "go do X on the desktop" to this dedicated agent, which runs its own screenshot→act loop.
- **C**: Hybrid — Ring0 orchestrates (decides *what* to do on the desktop), but the actual screenshot→act execution loop runs through a purpose-built agent with the native tool type.

**2. Who drives the loop?**
In Anthropic's computer use demo, the agent runs an autonomous loop: screenshot → decide → act → screenshot → repeat until done. Should Ring0 itself run this loop, or should there be a dedicated "desktop agent" subprocess that Ring0 delegates to? Ring0 doing it means every screenshot goes through Ring0's context window (which also handles voice, session orchestration, etc.). A dedicated agent keeps the context clean.

**3. What does "pluggable agent architecture" mean concretely?**
- A node can run something other than Claude Code? (e.g., a node that runs UI-TARS as its primary agent)
- Or a Claude Code node can *also* have UI-TARS available as a sub-capability?
- How does the UI surface this? Different node types? A "backend" dropdown when creating a node?

---

## Scope and Capabilities

**4. What tasks should Milestone 1 handle?**
Basic click/type automation, or complex multi-step workflows like "open Chrome, go to GitHub, create a PR"? This affects how much to invest in screenshot→act loop reliability vs. just exposing the primitives.

**5. Who triggers computer use?**
Does Ring0 autonomously decide to use the desktop (e.g., user says "check the Grafana dashboard"), or does the user explicitly request it (e.g., "use the desktop to open Chrome")?

**6. Should individual Claude Code sessions also have computer use?**
Or is this Ring0-only? A session working on code might want to open a browser to check docs.

---

## Image Delivery and Performance

**7. Have you verified that Claude Code's MCP client renders ImageContent?**
MCP supports `ImageContent` with base64 images, but it's unclear if the Claude Code CLI properly handles image content returned from MCP tools and passes it to the model. If not, the fallback is saving to a file and having Claude use `Read` to view it.

**8. Resolution**
Anthropic recommends 1024x768 or 1280x800 for their computer use tool (smaller = better coordinate accuracy). The Xvfb displays are 1920x1080. Should we downscale screenshots for the MCP tool, set the Xvfb resolution lower, or just use 1080p and see how well Claude handles it?

**9. Latency budget**
The screenshot→tunnel→hub→MCP→Claude→act round trip could be 3-5+ seconds per step. Is that acceptable, or do you want to optimize for speed?

---

## Milestone 2 & 3 Forward-Looking

**10. UI-TARS — local or API?**
There's an RTX PRO 6000 + RTX 5090 available. Is the plan to run UI-TARS locally on GPU, or use an API? Which version (UI-TARS-1.5? UI-TARS-2.0)? This affects whether the GPU Docker images need ML inference deps.

**11. How do you envision Claude + UI-TARS working together?**
Claude as the planner and UI-TARS as the executor? Claude decides "we need to navigate to the settings page" and UI-TARS figures out *how*? Or something else?

**12. For M3 (video agent) — what model are you thinking?**
A VLM with video input? Something custom? Rolling window of frames or actual video tokens?

---

## Integration and UX

**13. Should the browser user see Ring0's computer use happening?**
If someone has the Desktop tab open watching the virtual display, should they see Ring0 clicking around? Any need for visual indicators like a cursor highlight or action overlay?

**14. Input conflict**
If a human is interacting with the desktop via WebRTC while Ring0 is also doing computer use, whose inputs win? Should there be a locking mechanism, or is "last click wins" fine for now?

**15. Desktop app launching**
Should Ring0 be able to launch apps on the desktop as part of computer use (e.g., start Chrome, open VS Code)? Or does it just interact with what's already open?
