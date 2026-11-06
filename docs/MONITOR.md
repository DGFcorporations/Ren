# Ren stdout monitor

A small Streamlit interface for **the offline Ren process started by this launcher**.
It captures stdout and stderr asynchronously and updates a read-only text area about
twice per second. It does not attach to arbitrary PIDs or already-running services.

## Install and launch on a compatible local machine

The tested reference is Python 3.12/Linux. The Python dependency lock is for that
reference, not a claim of Windows/macOS qualification.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --require-hashes -r requirements-monitor.lock
pip install --no-deps -e .

# Explicit browser-view mode:
ren-monitor --web

# Native desktop webview mode, only with a working graphical environment
# and the pywebview renderer dependencies required by that OS:
ren-monitor
```

Default local addresses are Streamlit `127.0.0.1:8501` and Ren `127.0.0.1:8765`.
The launcher prints the local monitor address. A screenshot is not a deployed app;
these addresses are usable only where the processes are actually running.

```bash
ren-monitor --web --monitor-port 8511 --ren-port 8877 --data-dir ./synthetic-monitor-data
ren-monitor --web --no-autostart
```

If either selected port is occupied, the launcher refuses to attach or kill the
existing process. Select explicit free ports instead. The existing Ren data-directory
lock still prevents a second process from taking over an active ledger.

The desktop extra installs pywebview's Python launcher. Native GUI/web-engine system
dependencies and a graphical display are still required. This build environment is
headless: the native entry point explicitly rejects startup before spawning children.
**A real native window has not been opened/validated here.** Browser-mode UI tests are
not native-window tests. Windows owned-process cleanup is unsupported in this prototype;
other desktop configurations need their own qualification.

## What appears in the monitor

- Unbuffered Ren stdout and stderr, combined in stream order.
- Startup/shutdown diagnostics and optional sanitized `REN_EVENT` state observations.
- Process state, owned PID, exit code, generation and dropped-line count.
- Start, Stop and Clear-view controls for the launcher's owned child only.

Ren's `--console-events` flag emits event names, run/action IDs and durable state—not
fact values, draft text, approval digests, session capabilities or provider keys.
The durable ledger remains authoritative; console logging is a best-effort observer,
and observer failure cannot approve, fail or replay a task.

**Running means the process is alive, not that every feature is ready.** Current Ren
is G1: its transcripts/planner are scripted and its audio is a test tone. No live
model response stream exists to display until a separately approved integration is built.

## Architecture

```text
launcher
  ├── asyncio owned-process monitor ── Ren subprocess (-u)
  ├── authenticated loopback bridge ── fixed snapshot/start/stop/clear operations
  └── isolated Streamlit subprocess ── read-only dynamic text area
                                       └── optional native pywebview window
```

The launcher—not Streamlit Session State—owns both child processes. Fragment reruns,
page reloads and additional UI sessions do not create additional Ren processes.
The bridge's per-launch local capability remains in the Streamlit backend environment;
it is not put in a URL, browser JavaScript or captured output.

The optional native shell opens only the owned Streamlit URL and exposes no Python
`js_api` to page scripts. Its API/lifecycle contracts were tested with a stub; native
rendering and OS window behavior remain unverified.

## Bounds and lifecycle

- Default retained tail: 1,000 lines, 100,000 characters, 8,192 characters per line.
- Incremental UTF-8 decoding handles chunks that split a character. Partial lines
  are visible; final unterminated output is flushed on process exit.
- Long/older output is explicitly truncated; this is a live tail, not a full log archive.
- ANSI/control sequences are removed. Obvious authorization/key/password fields are
  redacted on a **best-effort** basis, not a comprehensive secret-discovery guarantee.
- Clear view only clears the retained display buffer; it does not clear Ren's data.
- Stop/close signals only owned process groups. Graceful termination is bounded,
  followed by escalation when needed; exit codes and unconfirmed cleanup are visible.
- Normal CLI SIGINT/SIGTERM, window exit, start failure and repeated stop/close have
  cleanup paths. Cleanup failures cause a nonzero launcher result.
- Forced SIGKILL/`os._exit()` of the launcher, or descendants deliberately escaping the
  owned process group, cannot be guaranteed by normal user-space cleanup. This is not
  an operating-system supervisor or production availability service.

Closing a browser tab in `--web` mode does not stop the launcher. Use Stop Ren for the
owned Ren child or Ctrl-C in the launcher terminal to shut down the complete local
monitor. Native window exit returns through the launcher's cleanup path, but that
actual native behavior still needs desktop validation.

## Hardening

- Fixed argument arrays; no shell, arbitrary command UI, PID attachment or stdin relay.
- A tightly filtered child environment; provider credentials and dotenv loading are
  not inherited by the monitor's subprocesses.
- Streamlit runs under a launcher-owned temporary HOME/CWD and an explicit empty
  secrets file, preventing implicit discovery of the user's Streamlit secrets/config.
- Strict `server.allowedHosts=127.0.0.1`, enabled CORS/XSRF and fixed loopback binding.
- Known Streamlit internal/public-IP discovery helpers are disabled in the pinned
  bootstrap, so rejected origins do not trigger that external-discovery fallback.
  This narrows defaults rather than disabling cross-origin protection.
- No inherited HTTP proxy for the local control bridge; redirects are rejected rather
  than forwarding the local capability elsewhere.
- The control API accepts only a small empty JSON command body and no caller argv.
- No app content is rendered from child logs as HTML. Log styling uses fixed CSS only.
- Framework telemetry, file watching, static directory serving and deployment toolbar
  controls are disabled/minimized for the local monitor.

The monitor is a single-operator localhost application, **not production user
authentication or a hardened public dashboard**. Do not expose it via a public tunnel.
A hostile process with access to the same user account is outside this trust boundary.

## WebRTC and live services

WebRTC is not needed to capture stdout. Bidirectional audio/video would be a separate
feature with its own transports, permissions, synchronization, lifecycle and tests.
It is not implemented or enabled here. There is no microphone/camera access, live
speech/model/memory provider, phone control or SMS integration in this monitor.

## Tests

```bash
ruff check src tests
ruff format --check src tests
pytest -q                         # full core + monitor suite, with monitor dependencies
pytest -q -m 'not monitor'        # core G1 only
pytest -q -m monitor              # monitor feature tests
```

See [MONITOR_VERIFICATION.md](MONITOR_VERIFICATION.md) for actual results and remaining
limitations. Test or dependency installation can download public packages; the runtime
monitor itself is not permission to call live providers or deploy services.
