# Stdout monitor verification — 2026-11-06

## Outcome

The local Streamlit stdout monitor is implemented and exercised against the actual
owned G1 Ren subprocess. **Native pywebview window rendering remains unverified**:
this execution environment has no graphical display. Its native entry point was
run and returned an explicit headless-runtime error before starting children.
No live speech, model, semantic-memory, phone, SMS or WebRTC service was enabled.

## Verified behavior

- Real Ren stdout and stderr reached the Streamlit text area without a page reload.
- A synthetic Ren memory proposal and denial generated actual sanitized process
  events; the proposed fact value and approval digest were absent from those events.
- Start/Stop/Clear controls worked through the authenticated loopback bridge.
- Reloading the Streamlit page did not duplicate or restart the Ren process.
- Losing the bridge produced a visible error rather than spawning a substitute child.
- The actual web-mode launcher was terminated with SIGTERM while Ren held a pending
  owned-browser fixture. Ren, Streamlit and recorded owned browser descendants stopped;
  an unrelated test listener remained available.
- Concurrent/repeated process starts, stops, restarts, stdout/stderr interleaving,
  split UTF-8, partial lines, oversized lines, bursts, exit codes, missing executables,
  held pipes, ANSI removal and best-effort secret-field redaction were exercised.
- Busy ports are rejected; the launcher does not attach to or terminate the service
  already using them.
- No external UI request or JavaScript error occurred in the browser monitor tests.

The refresh interval is configured to 0.5 seconds while the Streamlit session is
active. A small local update observation is not an SLA, a p95 benchmark, or proof
of behavior on a phone, remote network or native web engine.

## Final regression run

**263 test cases passed plus 143 unittest subtests; zero failures, errors or skips.**
Elapsed time was approximately 150.1 seconds. JUnit reports 406 when it includes
subtests; this is not 406 independent end-to-end scenarios.

| Group | Cases |
|---|---:|
| Existing G1 core suites | 181 |
| Async owned-process capture | 54 |
| Monitor bridge | 11 |
| Shell/launcher contracts | 6 |
| Real Streamlit browser UI | 3 |
| Actual launcher CLI lifecycle | 1 |
| Console event observation | 2 |
| Monitor hardening regressions | 5 |

The native shell tests use a stub for GUI lifecycle contracts and an actual
headless-rejection path. They must not be represented as a real desktop-window test.
Ruff lint/format checks, existing JavaScript syntax checks, and the application
wheel/module/static-asset packaging checks passed.

## Review findings fixed before commit

| Finding | Fix and regression |
|---|---|
| Framework accepted arbitrary WebSocket Host headers | Explicit loopback allowedHosts; an actual foreign-Host/Origin WebSocket handshake is rejected |
| Framework could rediscover credentials removed from its environment | Launcher-owned temporary HOME/CWD and explicit empty secrets file; synthetic poisoned-config regression confirms no key is loaded |
| Origin checking could use external IP discovery | Pinned bootstrap disables internal/public-IP discovery helpers while preserving CORS/XSRF; bad origins fail without that discovery fallback |
| Launcher could report cleanup success when a monitor recorded failure | Inspect cleanup states as well as exceptions; propagate unresolved ownership and nonzero CLI exit while closing remaining resources |
| Bridge thread-start failure could hang shutdown | Exception-safe socket reset and no shutdown call on a never-started server; injected-start-failure test returns promptly |

Additional controls include rejected bridge redirects, bounded empty command bodies,
fixed argv/no shell, no stdin relay, hidden backend-only control capabilities,
filtered child environments and static-only log CSS.

This is a bounded implementation review, not a complete security certification or
proof against a hostile process with the same OS-user privileges.

## Versions and environment

- Python 3.12.16; Streamlit 1.66.1; pywebview 6.2.1.
- Linux x86_64, Amazon Linux 2023; no DISPLAY, WAYLAND display or Xvfb was available.
- Playwright 1.64.1 with sandbox-enabled Chromium 156.0.8078.4 (revision 1248).
  Playwright uses its Ubuntu fallback build on this not-officially-supported host.
- `requirements-monitor.lock` captures the tested Python monitor environment with
  hashes while retaining the G1 core version constraints. It does not supply an
  OS display server or qualify GTK/Qt/Cocoa/WebView2.
- Package setup downloaded public dependencies. Runtime testing used owned local
  processes, synthetic data and loopback services, not real target accounts.

## Still not verified or implemented

- A real native pywebview window on the user's desktop, including its chosen renderer.
- Windows process lifecycle support; other desktop/mobile platforms need qualification.
- Bidirectional voice/video, microphone/camera capture or a WebRTC pipeline.
- Live providers, external account actions, deployment or unattended availability.
- Guaranteed cleanup after SIGKILL/host crash or a child deliberately escaping its group.
- Comprehensive log data-loss prevention; the live tail is bounded and redaction is best effort.

All validation servers and owned test children were stopped. No public app was
published, no hosted CI was started, and nothing was merged to main.
