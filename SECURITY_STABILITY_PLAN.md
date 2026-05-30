# vibr8 Security And Stability Plan

This document records the current product guardrails and implementation direction from the architecture review interview.

## Current Decisions

- vibr8 is intended to be Internet-accessible and hosted, not a localhost-only toy.
- Authentication is required outside explicit dev/test modes.
- No-auth startup must be refused unless an explicit environment variable is set.
- No-auth dev/test mode must bind to localhost only.
- Self-node mode is the only intended execution path. Local and remote nodes should use the same node tunnel model.
- Legacy in-process hub execution is migration debt and should be removed once parity is confirmed.
- Nodes are trusted, privileged execution environments.
- An authenticated user with access to a node gets full node capability for now: filesystem, terminal, git, coding agents, desktop, Android, and voice.
- Node credentials should be revocable tokens with metadata and user ownership.
- Node registration should require an authenticated browser user to approve/create the node token first.
- Public endpoints should be minimized to user authentication, second-screen pairing, and node pairing/registration flows that genuinely need unauthenticated bootstrap access.
- Second-screen and node onboarding must keep working, but their public paths need explicit threat review.
- File access can remain arbitrary on a node once the user is authenticated and authorized for that node.
- vibr8 should not add broad filesystem warning banners for sensitive paths at this stage.
- Coding-agent approvals are delegated to each backend's permission system for now.
- Ring0 is expected to participate in orchestration and approval workflows over time. The Ring0 session for a given node should maintain relevant operational memory for that node.
- Heavy ML/media test jobs should be opt-in but easy to run.
- The first stable test target should be `make test-core`, excluding voice/WebRTC/desktop/Android unless explicitly selected.
- Priority order: security hardening, stability, then completeness/orthogonality.

## Phase 1: Security Baseline

Goal: make accidental Internet exposure without auth impossible, while preserving pairing/onboarding flows.

- Add a startup guard:
  - If auth is disabled and `VIBR8_ALLOW_NO_AUTH=1` is not set, refuse to start. **Done.**
  - If auth is disabled and no-auth is explicitly allowed, force bind host to localhost unless another explicit dev override is set. **Done.**
  - `VIBR8_HOST` controls the bind host. Auth-enabled servers may bind publicly. No-auth servers require both `VIBR8_ALLOW_NO_AUTH=1` and `VIBR8_ALLOW_PUBLIC_NO_AUTH=1` to honor a public bind host such as `0.0.0.0`.
- Inventory every public route:
  - `PUBLIC_PREFIXES`
  - `PUBLIC_EXACT_PATHS`
  - public WebSocket paths
  - static asset paths
- Classify public routes into:
  - required for login
  - required for pairing/bootstrap
  - required static assets
  - should be authenticated
- Remove broad public access from Ring0 routes.
- Narrow node listing and activation to authenticated callers. **Done for
  `/api/nodes` and `/api/nodes/{node_id}/activate`; browser direct selection
  remains on authenticated `/api/clients/{client_id}/active-node`.**
- Review second-screen pairing endpoints for:
  - rate limiting **Pinned for unified pairing and legacy second-screen code requests.**
  - one-time code semantics **Pinned for native and second-screen token delivery.**
  - token delivery lifetime **Expired code behavior is pinned.**
  - replay protection **Pinned for repeated status polls after token delivery.**
  - auditability **Pinned for rate limits, brute-force cooldowns, confirmation outcomes, and token delivery.**
- Review node registration/tunnel endpoints for:
  - authenticated token creation
  - revocation
  - node identity binding
  - failed-auth logging **Pinned for registration rejection and node WebSocket tunnel rejection.**
  - reconnect behavior after revocation **Done for token-bound nodes; revocation closes bound online node WebSockets and blocks reconnect.**

## Phase 2: Revocable Node Tokens

Goal: replace ad hoc node API keys with user-owned, revocable credentials.

- Model node tokens similarly to device tokens:
  - token id
  - owning user
  - node name
  - created timestamp
  - last used timestamp
  - revoked timestamp/status
  - token signature/hash only, not plaintext token
- Add API routes for authenticated users:
  - create node token **Done as `/api/nodes/tokens`, with legacy `/api/nodes/generate-key` kept as an alias and audit logging pinned.**
  - list node tokens **Done as `/api/nodes/tokens`, scoped to the authenticated user.**
  - revoke node token **Done as `/api/nodes/tokens/{key_id}`, preserving metadata as revoked with success/failure audit logging pinned.**
  - rename/update node metadata
- Require nodes to connect with a valid non-revoked token. **Done for nodes registered after token-id binding; legacy nodes without a token id retain stored-key behavior.**
- Ensure revocation disconnects or blocks reconnect for the node. **Done for token-bound nodes: revocation marks matching online nodes offline and blocks reconnect via the stored node credential.**
- Keep capability model simple for now: if token is valid and user has access, the node has full capability.

## Phase 3: Self-Node Only

Goal: remove dual local execution paths so local and remote nodes share one debugging surface.

- Make self-node path mandatory in normal operation.
- Remove or quarantine `VIBR8_DISABLE_SELF_NODE` legacy mode.
- Move remaining hub-owned execution behavior behind node operations or delete it after parity.
- Ensure local self-node and remote nodes use the same interface contract for:
  - session create/list/kill/relaunch
  - file operations
  - git operations
  - terminal operations
  - Ring0 operations
  - desktop/computer-use operations
- Add regression tests proving local self-node and remote-node command handling follow the same contract.

## Phase 4: Test Matrix

Goal: make the default test command trustworthy and fast.

- Add `make test-core`.
- Ensure `test-core` installs/runs with dev dependencies and no optional media/ML stack.
- Move tests requiring optional dependencies behind explicit targets:
  - `make test-desktop`
  - `make test-voice`
  - `make test-webrtc`
  - `make test-android`
  - `make test-e2e`
- Configure pytest so async tests never silently skip because `pytest-asyncio` is missing.
- Add markers for optional suites.
- Make collection failures clear when an optional suite is requested without its dependencies.
- Update `make test` to either run `test-core` or run a documented aggregate that is expected to pass in the default environment.

## Phase 5: Reliability Cleanup

Goal: reduce hidden failure modes and make operational state easier to reason about.

- Replace sync methods that call `asyncio.ensure_future` without a running loop with explicit async APIs or injected task spawners.
- Add structured timeout behavior to node tunnel commands:
  - retry where safe
  - surface ambiguous state to callers
  - log late responses with request ids
- Split oversized modules along existing boundaries:
  - route groups out of `server/routes.py`
  - client registry/session state/broadcast fanout out of `WsBridge`
  - backend-specific launchers out of `CliLauncher`
- Replace shell-based git helper calls with argument-list subprocess calls where feasible.
- Add shutdown tests for background tasks, self-node process termination, and pending saves.

## Open Questions

- What exact role should vibr8-level approval memory play once Ring0 participates in approval workflows?
- How should user-to-node sharing work when nodes become scoped to users but can be granted to other users?
- Which Ring0 operations should be node-local only versus hub-global?
- What audit log is required for Internet-hosted operation?
