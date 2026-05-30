# Public Authentication Surface

This inventory records the routes that `server.auth.auth_middleware` allows
without a valid user, device, or service token when auth is enabled.

## Middleware Rules

Auth is bypassed for:

- Paths matching `PUBLIC_PREFIXES`.
- Paths matching `PUBLIC_EXACT_PATHS`.
- Paths matching `_PUBLIC_PATH_PATTERNS`.
- Non-API, non-WebSocket paths, so the SPA shell and static files can render
  the login page.

All other `/api/` and `/ws/` paths require a valid cookie, bearer token, or
`?token=` query value.

## Current Public Prefixes

| Rule | Classification | Current Reason | Follow-Up |
|---|---|---|---|
| `/ws/cli/` | node/session bootstrap | Claude CLI sessions connect back to the server on this path. | Revisit after self-node-only mode; CLIs should connect to the node-local server rather than the hub. |
| `/ws/node/` | node bootstrap | Remote nodes connect over outbound WebSocket and authenticate inside the node handler with node credentials. | Keep only if node tunnel auth remains handler-level; add explicit failed-auth logging and revocation semantics. |
| `/api/auth/login` | login | Browser needs to submit credentials before it has a session cookie. | Keep public. |
| `/api/auth/me` | login/session discovery | Browser needs to determine whether auth is enabled and whether the current cookie is valid. | Keep public, but ensure it never leaks private user data. |
| `/api/pairing/request` | device pairing bootstrap | Native and second-screen devices need to request a code before having a token. | Keep public with rate limits. |
| `/api/pairing/status/` | device pairing bootstrap | Devices poll this path while waiting for user approval. | Keep public with brute-force protection and one-time token delivery. |
| `/api/nodes/register` | node bootstrap | Nodes register with an API key in the request body. | Replace with authenticated user-created revocable node tokens. |
| `/api/second-screen/pair-code` | second-screen bootstrap | Legacy second-screen pairing requests a code before having a token. | Keep or replace with unified `/api/pairing/request`; preserve second-screen onboarding. |
| `/api/second-screen/status` | second-screen bootstrap | Second screens poll pairing status and receive a pending device token once. | Keep public only for status/token delivery; verify replay behavior. |
| `/assets/` | static asset | Built frontend assets must load before login. | Keep public. |
| `/sw.js` | static asset | Service worker file. | Keep public if service worker remains enabled. |
| `/manifest.json` | static asset | Browser app manifest. | Keep public. |
| `/logo` | static asset | Icon/logo assets. | Keep public. |
| `/favicon` | static asset | Browser icon assets. | Keep public. |
| `/apple-touch-icon` | static asset | Mobile icon assets. | Keep public. |

## Current Public Exact Paths

| Rule | Classification | Current Reason | Follow-Up |
|---|---|---|---|
| `/api/nodes` | risky/public metadata | UI currently reads node list without credentials. | Should become authenticated; second-screen bootstrap should use a narrower path if needed. |

## Tightened Routes

| Rule | Previous Classification | Current Auth Requirement | Notes |
|---|---|---|---|
| `/api/ring0/` | highest-risk public control surface | Valid user, device, or service token. | Ring0 MCP uses the `VIBR8_TOKEN` bearer token issued by `AuthManager` and passed to MCP by `Ring0Manager._get_service_token`; remote nodes forward hub Ring0 calls with their hub-issued service token. |

## Current Public Path Patterns

| Rule | Classification | Current Reason | Follow-Up |
|---|---|---|---|
| `^/api/nodes/[^/]+/activate$` | risky/public control surface | Lets callers switch a browser client's active node, used by voice/Ring0 flows. | Should require authenticated browser, device, or service token; clarify second-screen needs before tightening. |

## WebSocket Routes

| Route | Public Today? | Classification | Follow-Up |
|---|---:|---|---|
| `/ws/cli/{session_id}` | Yes | node/session bootstrap | Revisit with self-node-only path. |
| `/ws/node/{node_id}` | Yes | node bootstrap | Keep only with strong node credential validation. |
| `/ws/browser/{session_id}` | No | browser session | Keep authenticated. |
| `/ws/native/{client_id}` | No | device control | Keep authenticated via token after pairing. |
| `/ws/terminal/{session_id}` | No | terminal control | Keep authenticated. |
| `/ws/playground/{client_id}` | No | voice playground | Keep authenticated. |
| `/ws/enrollment/{client_id}` | No | voice enrollment | Keep authenticated. |

## Tightening Order

1. Move node registration from anonymous API-key-in-body bootstrap toward
   authenticated, revocable, user-owned node tokens. The authenticated
   `/api/nodes/tokens` create/list/revoke API is now in place while legacy
   `/api/nodes/register` remains public for compatibility. Token-bound nodes
   persist the issuing token id; revocation marks matching online nodes
   offline and blocks reconnect through their stored node credential. Legacy
   nodes without a persisted token id retain stored-key behavior until
   re-registered. Pre-migration ownerless keys are visible and revocable by
   any authenticated user so operators can clean up legacy credentials after
   upgrade.
2. Narrow node listing and activation to authenticated clients while preserving
   second-screen and voice routing workflows.
