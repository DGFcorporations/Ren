# Local stdout monitor implementation contract

Target: DGFcorporations/Ren, existing pilot/voice-memory-browser branch. Keep the application G1/offline, no live providers or target accounts, no deployment/merge, and omit .github workflows from commits as previously authorized.

This is stdout/stderr monitoring, NOT WebRTC or bidirectional agent interaction. No microphone, camera, stdin relay, arbitrary command UI, provider SDK, or public interface.

## Ownership

- The launcher owns the Ren process monitor, a token-protected loopback control bridge, and a Streamlit subprocess.
- Streamlit renders logs and invokes fixed start/stop/clear commands through that bridge. It never creates another Ren subprocess on a rerun.
- The optional pywebview shell displays the owned loopback Streamlit URL, with no Python JS API exposed. Closing it stops only owned children. Headless environments fail explicitly for desktop mode; web mode is a separate choice.
- GitHub/source-control operations remain with the main agent. Never inspect credentials or another process's memory/environment.

## Assigned core file API

Implement process_monitor.py and tests/test_process_monitor.py only when assigned.

`safe_child_env(extra: dict[str,str]|None=None) -> dict[str,str]`:
- Whitelist only runtime variables needed for PATH/HOME/temp/locale/platform display, not provider credentials, cloud tokens, SSH agents or .env loading.
- Force PYTHONUNBUFFERED=1 and disable Streamlit usage telemetry.
- extra is an internal caller-provided mapping, never exposed to HTTP/UI; only allow internal REN_MONITOR_* settings plus explicit PYTHONPATH if used by our test/source runner. Reject other keys instead of inheriting arbitrary credentials.
- Do not print environment values.

`OwnedProcessMonitor(command: tuple[str,...], *, cwd: Path|None=None, env: dict|None=None, max_lines=1000, max_chars=100000, max_line_chars=8192, stop_timeout=4.0)`:
- Fixed argv, no shell=True. Caller builds safe Ren argv; UI never supplies a command.
- Constructor starts a dedicated asyncio-loop thread, not the child process. Async subprocess stdout and stderr are combined and consumed in bounded chunks using incremental UTF-8 decoding. Do not block on unbounded readline().
- Own child only; start_new_session on POSIX, stop/terminate/kill only its owned process group. Never attach to a caller PID or kill another service on a busy port.
- Any inability to spawn or clean up must be visible in state/logs, not a fake success.

Methods:
- start() -> concurrent.futures.Future[bool]. Async idempotent startup; repeated start while active returns False and does not create another process. Restart permitted only after prior capture/cleanup finishes.
- stop() -> Future[bool]. Async bounded graceful stop then escalation for owned children; already stopped is harmless. Streamlit UI should remain responsive and poll the state.
- snapshot() -> dict with state (idle/starting/running/stopping/exited/failed/closed), pid (int|null), returncode (int|null), generation (int), text (str), total_lines (int), dropped_lines (int), revision (int), truncated (bool), error (str|null).
- clear() -> None clears displayed buffered logs only, not the child or its source data.
- close() -> None idempotent shutdown; stop owned child, drain/finish readers, stop loop and join threads. Bounded; no abandoned child after ordinary close/start failure.

Other requirements:
- Preserve non-ASCII output across chunk boundaries.
- Surface partial-line progress in snapshots, and flush last unterminated line on exit.
- Bound per-line and total retained text, indicate truncation/dropped output.
- Strip ANSI/control escapes; best-effort redact obvious Authorization/API-key/password fields without reading secrets from the environment. Do not call that comprehensive redaction.
- Distinguish requested stop from process crash and preserve exit code.
- Prevent prior-generation capture from overwriting new-generation status.
- Handle pipe flooding, simultaneous stdout/stderr, a descendant holding a pipe after its parent exits, repeated start/stop/clear, nonexistent executable and startup cancellation.
- Tests use owned Python fixture children, synthetic output only, no external network or provider calls. No dependency installations by the assigned worker.

## Main-agent modules

- monitor_bridge.py: ThreadingHTTPServer on 127.0.0.1, ephemeral port, strict Host and constant-time token authentication; fixed GET /snapshot and POST /start, /stop, /clear only; empty bounded JSON, no arbitrary argv or stdin. Token stays in Streamlit's backend environment and never enters a URL/browser JS/log.
- monitor_app.py: st.fragment(run_every=0.5), read-only dynamic st.text_area, state/exit/truncation indicators, Start/Stop/Clear controls and explicit G1/unverified labels. No blocking UI read loop or automatic child respawn on rerun.
- monitor_launcher.py: Ren argv uses the same interpreter with -u and the offline CLI; starts Streamlit on loopback with telemetry/file watching disabled. --web for browser mode; optional native shell via pywebview, main-thread GUI and finally cleanup. Never silently substitute web mode for failed native mode.
- Core CLI may add opt-in sanitized console event output. Logging is an observer after durable transitions; observer failure must not change approval/execution semantics. Never log fact text, drafts, session capabilities or credentials.


## Implemented hardening beyond the initial contract

- Streamlit starts through monitor_bootstrap.py, not an unconstrained generic CLI.
- Native desktop rendering remains unverified in the headless runtime; browser-mode Streamlit is tested separately.
- Explicit loopback allowedHosts and CORS/XSRF remain enabled; automatic IP discovery is disabled in the pinned framework bootstrap.
- A private temporary HOME/CWD and explicit empty secrets file prevent inherited Streamlit secrets/config discovery.
- Runtime.close checks process-monitor cleanup states, not only raised exceptions; unresolved cleanup propagates to a nonzero CLI result.
- Bridge thread-start failure closes/reset the socket without calling shutdown on a never-started server.
- Captured output is never interpolated into HTML; the dark log pane uses constant CSS only.
