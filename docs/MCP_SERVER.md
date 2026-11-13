# Subsystem Contract: Ren MCP-over-HTTP (`/mcp`) for Cue

> [!IMPORTANT]
> **User Review Required:** Approve blast radius + NEVER rules before Phase 3 execution.
> Supersedes the first draft — Ren shipped `fb128d4`/`cc57dc0` while exploring:
> `ren-mcp` stdio bridge + confirm-gated REST surface already exist on
> `pilot/voice-memory-browser` @ `cc57dc0` (67/67 wren tests green).

## 0. Phase 1 Exploration Findings (7-Vector Diagnostic)

- **Architecture & Role:** `src/wren/mcp/server.py` is a hand-rolled stdio
  JSON-RPC bridge (protocol `2024-11-05`) that forwards 9 typed tools to the
  loopback REST API (`ren_status`, `ren_converse`, `ren_dictate`,
  `ren_screen_read`, `ren_paste_preview`, `ren_paste_commit`, `ren_browser`,
  `ren_files`, `ren_set_mode`). Stdio cannot reach a cloud agent — **Cue runs on
  Manus cloud computers**. The missing piece is an HTTP transport for the SAME
  tool surface. New code is a transport adapter only: reuse `TOOLS` +
  `dispatch()` verbatim — zero duplicated tool logic.
- **Edge Cases:** `dispatch()` raises `MCPError` → must surface as MCP
  `isError` content (same as stdio `handle()`); empty `ren_converse`/`ren_dictate`
  text → REST 400 → `isError`; MCP `notifications/*` → no response; batch JSON
  → per SDK semantics; `ren_paste_commit`/`ren_browser`/`ren_files write` keep
  their existing `confirm`/`approval_id` gates — HTTP transport must NOT weaken
  them.
- **Invariants:** (a) stdio `ren-mcp` behavior byte-identical — existing
  `tests/wren/test_ren_agent.py` stays green; (b) widget/REST/WS unchanged;
  (c) auth invariant: non-loopback AND proxy-forwarded requests require bearer
  token on every route including `/mcp`; (d) mutations stay confirm-gated end to
  end — the HTTP path cannot bypass `confirm:true`/`approval_id`.
- **Future Scalability:** Single-user assistant, low-rate calls. Streamable-HTTP
  via official `mcp` SDK (`mcp==1.30.0` — the 1.x line; the 2.x SDK renamed the
  FastMCP surface this transport builds on) gives session handling + spec
  compliance for Cue's MCP client.
- **Failure Modes:** `mcp` package absent → `/mcp` not mounted, warning logged,
  `/api/status` reports `mcp_http_mounted:false` (no silent dead endpoint);
  dispatch/REST failure → `isError` result; auth failure → 401 before MCP
  handling.
- **Neighbor Interactions (2nd Invocation Paths):** `ren-mcp` stdio and the new
  `/mcp` HTTP endpoint are TWO transports over ONE `dispatch()` — changes to
  tools land once. `/ws/connect` keeps its own inline auth check — the
  forwarded-header fix must cover it too or it stays a bypass when a tunnel
  lands. `dispatch()` calls `REN_URL` (default `127.0.0.1:8765`) — inside the
  app this is a loopback self-call, no token needed; env override still works.
- **Six-Month Sanity:** ~150-line transport module reusing the existing tool
  catalog. No generic proxy tool — the 9 typed tools ARE the contract surface.
  If REST endpoints change, `dispatch()` breaks loudly in both transports.

## 1. Domain & Responsibilities

- **Primary Responsibility:** Serve the existing Ren MCP tool catalog over
  streamable-HTTP MCP at `http://<host>:8765/mcp` so remote MCP clients (Cue via
  future public ingress; LAN/Tailscale agents today) can drive Ren.
- **Scope Boundary — IN:** HTTP MCP transport, auth inheritance +
  forwarded-header fix, `mcp_http_mounted` status flag, tests, docs.
- **Out of Scope:** public tunnel/ingress config (owner deferred), changes to
  tool semantics or REST endpoints, Android app, stdio `ren-mcp` (unchanged),
  G1 pilot package.

## 2. Knowledge Boundaries (Encapsulation)

- **Permitted Knowledge:** `wren.mcp.server.TOOLS`, `wren.mcp.server.dispatch`,
  `MCPError`, `REN_URL`; app auth token; `is_loopback_client`.
- **Forbidden Knowledge:** must NOT bypass `dispatch()` to call controller
  directly (keeps one tool path), must NOT read secrets, must NOT expose shell/
  arbitrary fetch, must NOT weaken confirm gates.

## 3. Strict Prohibitions ("NEVER" Rules)

> [!CAUTION]
> - MCP-HTTP must NEVER bypass or weaken auth: proxy-forwarded requests require
>   the bearer token exactly like direct non-loopback requests — on `/mcp`,
>   `/ws/connect`, and all REST routes.
> - MCP-HTTP must NEVER weaken mutation gates: `confirm`/`approval_id` semantics
>   identical to stdio transport.
> - MCP-HTTP must NEVER add a second port, daemon, or duplicate tool definitions.
> - MCP-HTTP must NEVER silently mount-fail: absent `mcp` package = warning +
>   `mcp_http_mounted:false` in `/api/status`.
> - MCP-HTTP must NEVER expose tools beyond the existing 9-tool catalog.

## 4. Boundary Schemas & Data Contracts

- **Endpoint:** `POST|GET|DELETE /mcp` — MCP streamable-HTTP transport.
- **Tools:** identical names/schemas/semantics as `wren.mcp.server.TOOLS`
  (9 tools). `tools/list` returns the same catalog; `tools/call` →
  `dispatch(name, arguments)` → JSON text content; `MCPError` → `isError:true`.
- **Auth:** `Authorization: Bearer <token>` or `X-Wren-Token` or `?token=` —
  same extraction as REST.

## 5. File Blast Radius

### ren-mcp-http subsystem
#### [NEW] src/wren/mcp/http.py
- **Responsibility:** build the streamable-HTTP MCP ASGI app reusing `TOOLS` +
  `dispatch()`; optional-import safe (`mcp` absent → returns None).
#### [NEW] tests/wren/test_mcp_http.py
- **Responsibility:** transport tests — initialize/tools-list/tools-call over
  httpx, auth 401 cases, isError mapping, confirm-gate preservation.
#### [MODIFY] src/wren/ui/orb_preview.py
- **Boundary Change:** mount `/mcp` when transport builds; treat requests with
  proxy headers (`cf-connecting-ip`, `cf-ray`, `x-forwarded-for`, `forwarded`)
  as non-loopback in BOTH HTTP middleware and `/ws/connect` handshake; add
  `mcp_http_mounted` to `/api/status`.
#### [MODIFY] pyproject.toml
- **Boundary Change:** add pinned `mcp` dependency.
#### [MODIFY] docs/REN_AGENT.md
- **Boundary Change:** document `/mcp` HTTP endpoint + Cue hookup notes.

## 6. System Invariants

- **Pre-condition:** `create_app(controller, auth_token)` with live controller.
- **Post-condition:** `POST /mcp` initialize + tools/list + tools/call work;
  stdio `ren-mcp` unchanged; all prior tests pass.
- **Invariant:** proxied requests without valid bearer → 401 on every route.

## 7. Failure Modes & Degradation

- `mcp` absent → no mount, warning, `mcp_http_mounted:false`.
- `dispatch()` error → `isError` content (never dropped session).
- Auth failure → 401 pre-protocol.
- Session/transport errors → SDK-standard JSON-RPC error responses.

## 8. Verification & Status Metadata

- **Automated:** `.venv/bin/pytest tests/wren -q` ; `.venv/bin/ruff check src tests`.
- **Manual/Live:** `curl -X POST http://127.0.0.1:8765/mcp` initialize;
  `tools/list` returns 9 tools; `tools/call ren_status` returns status JSON;
  `curl -H 'X-Forwarded-For: 1.2.3.4' http://127.0.0.1:8765/mcp` → 401;
  LAN without token → 401, with token → MCP response.
- **Status:** APPROVED (owner, 2026-11-13)
- **Downstream Consumers:** Cue cloud agent (after owner-approved public ingress),
  LAN/Tailscale MCP clients, `ren-mcp` stdio (unchanged).
