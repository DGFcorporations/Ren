# G1 offline pilot implementation contract

Repository: DGFcorporations/Ren. Branch: pilot/voice-memory-browser. Initial commit: 17b5d31a1b8425e60a69458a9bcf8fb8b663d400. Do not touch workbench-agent, any default branch, or any other repository.

Approved: a self-contained offline application and tests with synthetic fixtures. Forbidden: live provider credentials, paid model/speech calls, real target accounts, deployment, merge to main, arbitrary browser URLs, reading personal browser profiles. GitHub source-control operations are handled only by the main agent.

Working directory is /agent/workspace/ren-g1. Package src/workbench_pilot. Python interpreter .venv/bin/python. Main agent manages dependencies, controllers, API, UI, documentation, packaging and git writes. Assigned agents may edit only their assigned files and tests.

## Shared architecture

- stdlib SQLite stores synthetic facts/revisions/tombstones and durable run/action/approval state.
- The planner is deterministic and consumes fixture IDs, not live model output.
- Voice adapters provide scripted transcripts and a synthetic tone, NOT speech recognition or TTS. Never access the microphone or a real speech service in G1.
- Real Playwright/Chromium exercises only an owned HTTP fixture at one explicitly supplied loopback origin. No external networking, user profiles or arbitrary evaluate/command tools exposed to the app.
- One active browser action at a time. G1 server binds loopback. Existing v1's live model commands are not copied into this package.
- Record provenance and status accurately: real SQLite/HTTP/browser results distinct from mocked voice/model/semantic memory. Never call a contract test live integration validation.

## Ledger module contract (ledger.py)

Public classes LedgerError(ValueError), Conflict(LedgerError), StaleApproval(Conflict), NotFound(LedgerError).

Ledger(path: Path, *, clock: Callable[[],float]=time.time): creates its own SQLite schema, enables foreign keys/WAL, uses a connection protected by RLock. Do not reuse or modify any old Workbench DB.

Facts:
- seed_facts(owner: str, items: dict[str,str]) -> None. Fixture-only bootstrap; insert absent keys, do not overwrite existing values/tombstones.
- get_fact(owner,key) -> dict or None (fields key,value,revision,deleted,source). Normal retrieval excludes tombstones.
- get_fact_state(owner,key) -> owner-scoped revision metadata including tombstones; a deleted value remains null. This allows explicitly approved recreation with the correct CAS revision.
- list_facts(owner) -> list[dict] of active facts.
- search_facts(owner,query,limit=10) -> list[dict]. Bounded local keyword search, preferably FTS5; fall back explicitly to same-owner LIKE if FTS5 unavailable, not another dataset.
- set_fact(owner,key,value,*,expected_revision: int|None=None) -> dict. New revision, source synthetic_fixture; mutations after bootstrap are controller-approved actions.
- delete_fact(owner,key,*,expected_revision: int|None=None) -> dict. Tombstone/revision; stale values must not resurface.

Runs/actions:
- create_run(owner,request_id,payload: dict) -> tuple[dict,bool]. Canonical JSON payload digest, one row per (owner,request_id); duplicate identical request returns original row and False, different payload with same ID raises Conflict; new returns (row,True). Status initially preparing. Run/action identifiers are uuid hex strings. row fields id,owner,request_id,status,action_id nullable,proposal nullable,receipt nullable,reason nullable,cancel_requested bool.
- propose(run_id,kind,args: dict,binding: dict,*,ttl_seconds=300) -> dict. Persist one pending action for a preparing run. SHA256 canonical immutable proposal contains kind,args,binding; approval bound to this digest, owner/run and expiry. Return action fields id,run_id,owner,kind,args,binding,digest,status='pending',expires_at. Run status pending.
- get_run(owner,run_id) -> dict; wrong owner -> NotFound. Include decoded proposal and receipt.
- get_action(owner,action_id) -> dict; wrong owner -> NotFound.
- decide(owner,action_id,approved: bool,*,digest: str) -> dict. Only pending+unexpired+exact digest; one-shot decision. Reject stale/replayed/cancelled. Approved status approved, run approved; denial status denied/run denied.
- begin(owner,action_id,*,current_binding: dict) -> dict. Atomic approved->executing, once only; exact stored binding comparison; recheck expiry and cancel state. Changed context or expired approval becomes stale/expired, terminal and cannot execute. Return action row.
- complete(owner,action_id,receipt: dict) -> dict. executing->completed; store receipt atomically. A cancel request during execution is preserved as cancel_requested but does not erase truthful completed action evidence.
- fail(owner,run_id,reason,*,uncertain=False) -> dict. Keep terminal completed receipts; otherwise mark failed or uncertain and prevent replay.
- cancel(owner,run_id) -> dict. preparing/pending/approved -> cancelled with action status cancelled. Executing -> cancel_requested=True (do not falsely assert undo); terminal remains terminal.
- expire_run(owner,run_id) -> dict. Terminalize unused pending/approved actions after expiry; never undo executing or completed work.
- recover() -> int. On startup preparing/pending/approved -> interrupted, executing -> uncertain, invalidate approvals; never auto-resume side effects. Completed/denied/cancelled/stale remain terminal. Keep facts/receipts.
- events(owner,run_id) -> list[dict]. Durable event summary with timestamps, transition/action IDs; only synthetic/redacted values.
- close().

Use injected clock for expiry tests. Limits: keys<=120 chars, fact values<=4000, request ID<=100, proposal JSON<=32KB. Reject invalid types and missing owner/run/action. Add transactional concurrency and duplicate-decision tests. No network or credentials.

## Browser module contract (browser.py)

Public classes BrowserUnavailable(RuntimeError), FixtureViolation(ValueError).

FixtureBrowser(origin: str, *, headless=True): exact loopback http origin with numeric port; reject credentials, fragments, paths, foreign hosts. Use a dedicated ThreadPoolExecutor(max_workers=1) so Playwright objects stay on the same thread. Lazy start; no existing browser profile or cookies. Methods may be blocking for sync FastAPI endpoints. Main test dependencies include playwright 1.64.1; fail clearly if browser unavailable, no fake browser fallback.

- open(run_id: str, surface: str='textarea') -> dict snapshot. Allowed surfaces textarea, contenteditable, iframe, delayed. Navigates only {origin}/fixtures/draftbox?run_id=...&surface=... in a fresh anonymous context/page for each run. Cap open pages (e.g. 5); close terminal runs on request.
- snapshot(run_id) -> dict. Stable fields: origin,tab_id (use run_id),account='synthetic-account-a',recipient='synthetic-recipient-a' (or current select value),page_epoch (from document state),surface,before_text,save_count. Ignore transient rendering timestamps. Fail if page/context wrong/closed or URL outside allowlist. Parent controller adds fact binding.
- execute(run_id: str, text: str, *, expected_snapshot: dict|None=None) -> dict receipt. The controller always supplies the approved snapshot. Recheck the expected snapshot on the worker thread, fill the exact composer, arm the owned fixture's one-use save guard, click Save draft, and independently read back saved draft and save count. The fixture checks approved target/epoch/content atomically at its commit boundary; arbitrary third-party pages are not covered by that guarantee. No arbitrary selectors or scripts accepted from callers. text <=8000 chars. Return content,recipient,account,save_count,verified bool,origin,tab_id. A browser exception after a possible write must propagate so controller can mark uncertain.
- tamper(run_id,kind) -> dict snapshot. Only controlled fixture test modes: recipient (switch to synthetic-recipient-b), draft (change current editor text), reload (new page_epoch), close (close page then return {'closed':True}). No arbitrary URLs/evaluate strings.
- screenshot(run_id) -> bytes PNG of owned synthetic page.
- close_run(run_id), close().

Browser security: block service workers and all network requests other than exact configured origin and fixture asset paths. Block new popups/foreign navigations. Request routes allow only /fixtures/...; no app control API accessible from browser worker. No provider traffic, no browser extension/user-data-dir attachment. Do not modify platform security settings if launch fails; report limitations.

## Fixture static files

Assigned browser agent owns src/workbench_pilot/static/draftbox.html, draftbox.js, draftbox.css and optional frame.html.
Main app serves GET /fixtures/draftbox -> draftbox.html; GET /fixtures/assets/{name} -> matching files; iframe src /fixtures/assets/frame.html.

Fixture UI always shows 'SYNTHETIC FIXTURE — no messages sent'. Account and recipient have stable test IDs. Variants use textarea, contenteditable, iframe, or deterministic delayed enablement. JS stores the submitted draft and save_count in page state/DOM, not a remote service. page_epoch random per page load; snapshot stable until context changes. Save draft is a local fixture operation, never a real message send. Show escaped content using textContent; no external assets or third-party requests.

## Integration conventions

Owner for app is a fixed synthetic owner, not user-supplied identity. Main API adds CSRF/same-origin/loopback checks. RunId/requestId are synthetic identifiers. Real target-account functionality stays disabled and plainly labeled. No high-failure claims or use of 300 mock trials to claim real-device reliability.
