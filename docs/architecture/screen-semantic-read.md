# Subsystem Contract: wren.screen semantic read (desktop app content layer)

> [!IMPORTANT]
> **User Review Required:** This adds a content-level read of desktop apps on top of
> Ren's existing window-identity inspector. The source of truth is the optional
> `agent-desktop` CLI (audited 2026-11-10, OPS handoff). When the binary is absent,
> every surface silently degrades to today's identity-only context — zero regression.

## 0. Phase 1 Exploration Findings (7-Vector Diagnostic)
- **Architecture & Role:** `screen/inspector.py` stops at window identity (WM_CLASS + title + optional screenshot). The needed layer is content-level (element tree + text) — a thin adapter beside the inspector, not a new subsystem. Ren already shells out to `xdotool`/`xprop`/`import`; a subprocess adapter to `agent-desktop snapshot` matches that pattern exactly.
- **Edge Cases:** binary absent → `None` → identity-only; Electron apps without AX trees → `complete:false`, elements empty; secure/password fields → tool withholds values natively; oversized trees → cap at `MAX_ELEMENTS=400`/`MAX_DEPTH=12`; headless spawn (no DISPLAY) → existing try/except → `None`.
- **Invariants:** READ-ONLY lane — this path must never click/type/set (writes go through confirmed-paste only); snapshots carry private content → held in memory only, never logged, never written to disk; HTTP surface stays behind existing loopback+bearer auth.
- **Future Scalability:** per-read subprocess ~10–50ms; on-demand pulls only (no watcher/polling loop in v1); LLM injection bounded to ~40 actionable refs to keep prompts small.
- **Failure Modes:** missing binary/timeout/parse-fail/STALE_REF → degrade to identity-only `tree:null` — never 500 the endpoint, never crash converse.
- **Neighbor Interactions (2nd invocation paths):** FOUR consumers share this surface — orb UI (`/api/screen/*`), `converse()` screen_note, MCP `ren_screen_read`, phone bridge. Tree lands on `/api/screen/tree` + optional `deep` param on `ren_screen_read`; `converse()` gains a *bounded* desktop context line, additive only.
- **Six-Month Sanity:** one adapter file + one inspector method + one endpoint + one MCP param. If `agent-desktop` is abandoned, `pyatspi` swaps behind the same function signature — no ripples.

## 1. Domain & Responsibilities
- Primary Responsibility: Return a bounded, structured semantic tree of the focused desktop app's accessibility elements (roles, names, ref ids, actions) on demand.
- Scope Boundary: `wren.screen` read path only; HTTP + MCP + converse-context surfaces.
- Out of Scope: acting on elements (click/type — belongs to a future gated action lane, not this contract), phone-side a11y (Android orb handles its own), caching/watchers, installing the binary.

## 2. Knowledge Boundaries
- Permitted: focused-window identity (existing inspector), `agent-desktop` JSON envelope stdout, existing auth/auth-token gate.
- Forbidden: reading browser DOM (browser/session owns web), touching phone screen store, writing to any window, reading non-focused windows' content in v1.

## 3. Strict Prohibitions ("NEVER" Rules)
> [!CAUTION]
> - NEVER invoke `agent-desktop click|type|set-value|press` — this contract is read-only (`snapshot|find|list-apps|list-windows` allowlist only).
> - NEVER log, persist, or write snapshot JSON to disk/logs — memory-only, response-scoped.
> - NEVER hard-fail on absence of the binary or an app without an AX tree — degrade to `tree:null`.
> - NEVER include secure-field values — rely on the tool's native withholding AND assert `value` is absent on `secure:true` nodes in code.
> - NEVER bypass the existing bearer-token gate on the new endpoint.

## 4. Boundary Schemas & Data Contracts
### Inputs
- `GET /api/screen/tree` → no params (v1: focused window only)
- MCP `ren_screen_read` → optional `deep: boolean` (default false = today's payload)
- `ScreenInspector.read_active_tree()` → `app_name: str, window_id: str` → `ScreenTree | None`
### Outputs
- `ScreenTree { app, window_id, snapshot_id, complete: bool, ref_count: int, elements: [{ref_id, role, name, actions[]}], source: "agent-desktop" }`
- `GET /api/screen/tree` → `{ context: <existing identity fields>, tree: ScreenTree | null }`
- converse() context → ≤40 refs rendered as `APP: name — refs: [role "name", …]` inside the existing screen_note mechanism (desktop section additive, ≤1500 chars total stays).

## 5. File Blast Radius
### wren/screen
#### [NEW] `src/wren/screen/tree_reader.py`
- Responsibility: `read_tree(app_name, window_id) -> ScreenTree | None` — locates `agent-desktop` on PATH, runs `snapshot --app <name> -i --format json` (timeout 4s), validates envelope, maps to bounded `ScreenTree`, withholds secure values, returns `None` on any failure.
#### [MODIFY] `src/wren/screen/inspector.py`
- Boundary Change: add `read_active_tree()` — resolves focused app/window via existing path, delegates to `tree_reader.read_tree`.
### wren/ui
#### [MODIFY] `src/wren/ui/orb_preview.py`
- Boundary Change: add `GET /api/screen/tree` (identity + tree payload); token-gated identically to `/api/screen/capture`.
### wren/mcp
#### [MODIFY] `src/wren/mcp/server.py`
- Boundary Change: `ren_screen_read` accepts `deep` bool → calls `/api/screen/tree` and merges `desktop_tree` into response.
### wren/core
#### [MODIFY] `src/wren/core/controller.py`
- Boundary Change: `get_screen_tree()` passthrough; `converse()` appends bounded desktop-refs line to screen_note when a tree exists (additive section only).
### tests
#### [NEW] `tests/test_screen_tree.py`
- Responsibility: fake `agent-desktop` on PATH (fixture binary printing canned JSON); assert tree shape, caps, secure-value withholding, absent-binary → `None`, timeout → `None`, malformed JSON → `None`.
#### [MODIFY] `tests/` (existing web/MCP test module — extend where `ren_screen_read`/`/api/screen/*` coverage lives)
- Boundary Change: `deep` param + `/api/screen/tree` contract tests.

## 6. System Invariants
- Pre-condition: focused window resolves (existing inspector path) before any read.
- Post-condition: returned tree has `len(elements) ≤ 400`, every element carries `ref_id`+`role`, and no element has a `value` field on `secure` nodes.
- Invariant: with the binary absent, all surfaces return byte-identical payloads to today.

## 7. Failure Modes & Degradation
- Binary missing → `tree:null`, surfaces unchanged.
- Timeout/parse error/`PLATFORM_NOT_SUPPORTED` → `tree:null`, surfaces unchanged.
- App exposes no AX tree → `tree:{complete:false,elements:[]}` (truthful, not null — the read happened and found nothing).

## 8. Verification & Status
- **Automated:** `python -m pytest tests/test_screen_tree.py -xvs`; `python -m pytest tests/ -k 'screen or mcp' -x` (no regressions).
- **Manual/live:** `agent-desktop status`; focus gnome-calculator → `curl -H "Authorization: Bearer $TOKEN" localhost:8765/api/screen/tree` shows elements; `ren_screen_read{deep:true}` via MCP returns `desktop_tree`; asking Ren "what's on my screen" cites real elements.
- **Status:** UNDER_REVIEW
- **Downstream Consumers:** orb UI, MCP callers (fleet seats incl. Ace), converse().
