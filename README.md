# Workbench — G1 offline pilot

**Phone and workstation agent:** see [Ren agent runbook](docs/REN_AGENT.md). That is the installable Android orb, the workstation process, dictation, confirmed paste, screen reading, browser pointer, and the MCP bridge.

**Development branch:** `pilot/voice-memory-browser` in `DGFcorporations/Ren`.

A self-contained, local rehearsal of an approval-gated memory-to-browser workflow.
It deliberately contains **no live model or speech client, real-account browser
attachment, SMS capability, or deployment configuration**.

> This is not a live voice assistant. Transcripts are authored fixtures and the
> audio sample is a generated test tone. Speech recognition, speech synthesis,
> Pipecat, Mem0 semantic retrieval and actual-device integrations are **UNVERIFIED**.

## What actually works here

- A local web interface with three synthetic requests and four composer variants.
- Persistent SQLite facts, keyword search, revisions and deletion tombstones.
- Exact-action approvals tied to the run, local session, browser context and fact revision.
- A real, anonymous, sandbox-enabled Chromium worker that fills and clicks **only
  the owned local DraftBox fixture**, then reads the saved content independently.
- Textarea, contenteditable, iframe and delayed-control workflows.
- Durable approval decisions, expiry, request deduplication and truthful cancellation states.
- Restart recovery that invalidates pending approvals and marks interrupted execution uncertain.
- Controlled synthetic fault buttons for changed recipients, drafts, page state and memory revisions.
- A local test tone and scripted transcript adapter for exercising interface contracts.

None of this certifies control of arbitrary websites, phone apps, microphones,
other agents, or delivery to a real recipient. The fixture's “Save draft” writes
only page-local test state; the verified receipt is recorded in the local ledger.

## Run locally

The G1 reference is **Python 3.12 on Linux/POSIX**. Other platforms are unverified.
Use a dedicated checkout and data directory. Do not point the pilot at an existing
application database or private browser profile.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements-g1.lock
pip install --no-deps -e .
python -m playwright install chromium
workbench-pilot status
workbench-pilot serve
```

Open `http://127.0.0.1:8765` **on the same computer**. No API key or `.env` is needed
or read. The server does not bind to all interfaces and cannot be enabled for live
providers by an environment variable or command-line option.

Chromium must be able to launch with its sandbox enabled. On a supported test
machine, install the OS dependencies described by Playwright if needed. **Do not
solve a launch failure by disabling the sandbox or changing system protections.**
The app reports browser unavailability instead of silently substituting a fake
browser or a real user profile.

To move the fixture's port or storage:

```bash
workbench-pilot serve --port 8877 --data-dir ./another-synthetic-data-directory
```

Only one process may own a data directory. A process lock prevents another running
server from invalidating its approvals through startup recovery.

## Optional real-time stdout monitor

A Streamlit monitor now captures the **owned Ren subprocess's actual stdout and
stderr** into a bounded, read-only text area, refreshing about every 0.5 seconds.
It has Start/Stop/Clear-view controls and does not respawn Ren on UI reruns.

```bash
pip install --require-hashes -r requirements-monitor.lock
pip install --no-deps -e .
ren-monitor --web                 # explicit local browser view
ren-monitor                       # native pywebview shell, when a GUI/backend is available
```

The monitor is still G1/offline. No microphone, WebRTC audio/video, model provider,
real-account browser or deployment is enabled. The real Streamlit browser view was
tested; native desktop window rendering is **unverified** in this headless runtime.
See [monitor setup and limits](docs/MONITOR.md) and
[monitor verification](docs/MONITOR_VERIFICATION.md).

## Try the real local workflow

1. Choose a synthetic request and composer variant.
2. Click **Run fixture**. The controller retrieves a synthetic fact and opens its
   owned page in a fresh Chromium context.
3. Review the complete proposed text, target, recipient, account and fact revision.
4. Click **Approve once** or **Deny**. An approval expires after five minutes.
5. Inspect the observed receipt and managed-browser screenshot. These come from
   the worker, not an interactive copy of the page.
6. Repeat with **Change recipient**, **Reload page** or **Change fact revision**
   before approving. The stale approval must be rejected.

Use the memory panel to propose an approved synthetic fact update/deletion. The
bootstrap does not resurrect deleted fixtures. Run transitions survive restart;
old approvals do not silently execute after restarting.

The controlled fault interface is intentionally G1-only and operates solely on
synthetic fixture data. Disable it with `--no-fault-controls`. It must not be
carried into a live release as an unreviewed administrative backdoor.

## Cancellation and uncertain outcomes

Stopping a pending action prevents execution. Once execution has begun, a click
may already have happened: the system records the cancellation request and either
retains a verified completion or marks the outcome uncertain. It never claims to
have undone an operation it cannot undo. Reusing a request/approval ID cannot
execute the same action again. There is no automatic retry of a side effect.

A new, explicitly approved request is different from retrying an uncertain old
request; a future real-account implementation needs an external reconciliation
strategy before enabling such writes.

## Offline and security boundary

- “Offline” means **no live application providers or real target accounts**. Build
  setup downloads packages/browser binaries, and GitHub stores source code.
- The controller is a deterministic fixture planner, not an LLM. It does not
  interpret arbitrary user speech or instructions embedded in stored text.
- The browser uses anonymous contexts and an exact loopback origin/path allowlist.
  Foreign requests, redirects, WebSockets, popups, service workers and application
  control API access are blocked from the worker.
- The web interface has loopback origin/Host checks, same-origin protections,
  local session capabilities, one-use approvals, a 64-KiB body limit and a CSP.
- Microphone/camera access is disabled by policy. No Web Speech service is invoked.
- The local session capability is **not production user authentication**. All
  application data belongs to one synthetic owner; this is not a multi-user server.
- The owned fixture checks the approved target, document epoch and text atomically
  in its Save handler. Arbitrary third-party sites may not offer this transaction
  boundary and are not qualified by these tests.
- No arbitrary selectors, URLs, JavaScript evaluation or shell commands are exposed
  as caller-controlled actions. The worker uses fixed, reviewed fixture operations.
- A hostile process on the same computer is outside this prototype's trust boundary.
  Do not publicly expose the server or use it for sensitive data.

## Test and evidence

```bash
ruff check src tests
ruff format --check src tests
pytest -q -m 'not monitor' --tb=short  # G1 core only
# With requirements-monitor.lock installed, pytest -q runs the complete suite.
```

Full tests include real local Chromium, HTTP API/controller interactions, browser
UI flows and SQLite process-crash/concurrency checks. No provider credentials are
used. A browser skip is **unverified**, not a successful browser test.

For contract-only work when a sandboxed browser is unavailable:

```bash
pytest -q -m 'not browser and not monitor'
```

See [verification evidence](docs/G1_VERIFICATION.md) for the actual environment,
results, limitations and defects found/fixed during this build. Do not infer live
speech or general field reliability from the offline test count.

## Layout

```text
src/workbench_pilot/
  adapters.py     # scripted transcripts, deterministic text, generated PCM tone
  ledger.py       # owned SQLite schema, facts and durable approval/run state
  browser.py      # real Chromium, exact fixture allowlist and independent receipts
  controller.py   # one authoritative action path and safe restart/cancel semantics
  policy.py       # loopback settings and process ownership lock
  web.py, cli.py  # local-only UI/API and command line
  process_monitor.py, monitor_*.py  # owned stdout capture, Streamlit, native-shell launcher
  static/        # UI and owned DraftBox assets; no external CDN
fixtures/         # authored synthetic scenario catalogue
tests/            # contract, SQLite, HTTP, real browser and UI tests
```

The assistant retains its Workbench name; choosing the Ren repository did not
implicitly rename the character.

## Not wired in G1

- Live microphone recognition, speech synthesis, WebRTC/Pipecat sessions and a voice persona.
- Live LLM tool calling, embeddings, Mem0/Qdrant semantic retrieval or provider billing controls.
- Existing personal-browser attachment, device/phone control or real-site typing.
- SMS, other-agent/app connectors, purchases, public hosting, scheduled/unattended tasks.

Candidate live dependency versions are documented separately. Resolving metadata
or providing a fail-closed adapter boundary does not validate a live integration.
G2 requires a separate approval, integration work, secure credentials, privacy
choices and a test-spend limit. Nothing is merged to `main` by this G1 build.
