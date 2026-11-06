"""Bounded stdout capture for one fixed, owned, offline subprocess.

There is no shell, PID-attachment API, stdin relay, dotenv loader, or provider SDK.
The loop thread owns the entire subprocess lifecycle; UI threads only submit
requests and take locked snapshots. POSIX process groups are required, rather
than silently offering incomplete descendant cleanup on other platforms.

``close`` and ordinary Python interpreter exit clean up the owned process group.
A process deliberately escaping its session is outside that ownership boundary;
SIGKILL/os._exit of the monitor itself cannot run its atexit cleanup. On Linux,
termination checks distinguish dead zombies (reaped by init/the existing
subreaper) from executing descendants, without reading process environments.
Log redaction is deliberately best effort, NOT a secret-discovery/DLP system.
"""

from __future__ import annotations

import asyncio
import atexit
import codecs
import concurrent.futures
import contextlib
import math
import os
import re
import signal
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any

# Read ONLY these keys from the ambient environment. In particular, do not copy
# os.environ and then try to enumerate/remove the ever-growing set of secrets.
_RUNTIME_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "TMP",
        "TEMP",
        "TMPDIR",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_COLLATE",
        "LC_TIME",
        "LC_NUMERIC",
        "LC_MONETARY",
        "LC_MESSAGES",
        "LC_PAPER",
        "LC_NAME",
        "LC_ADDRESS",
        "LC_TELEPHONE",
        "LC_MEASUREMENT",
        "LC_IDENTIFICATION",
        "SYSTEMROOT",
        "SystemRoot",
        "WINDIR",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "XAUTHORITY",
    }
)
_FORCED_ENV = {
    "PYTHONUNBUFFERED": "1",
    "PYTHONNOUSERSITE": "1",
    "PYTHON_DOTENV_DISABLED": "1",
    "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
}
_INTERNAL_ENV = re.compile(r"REN_MONITOR_[A-Z0-9_]+\Z")
_AUTH_FIELD = re.compile(r"(?i)(\bauthorization\b[\"']?\s*[:=]\s*).*")
_SECRET_FIELD = re.compile(
    r"(?i)(\b(?:api[_-]?key|password|passwd|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|secret|token)\b[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*(?:\"|$)|'[^']*(?:'|$)|[^\s,;]+)"
)
_READ_CHUNK = 4096
_STREAM_LIMIT = 65536
_POLL_INTERVAL = 0.025
_KILL_TIMEOUT = 1.0
_DRAIN_TIMEOUT = 1.0
_STARTUP_ALLOWANCE = 1.0
_TRUNCATION = "… [truncated]"


def _extra_key(key: str) -> bool:
    return key == "PYTHONPATH" or _INTERNAL_ENV.fullmatch(key) is not None


def _validate_env(values: dict[str, str], *, extras_only: bool) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("Environment keys and values must be strings")
        allowed = _extra_key(key) or (not extras_only and (key in _RUNTIME_ENV or key in _FORCED_ENV))
        if not allowed or "\x00" in key or "\x00" in value or "=" in key:
            # Neither keys nor values are echoed: callers can accidentally put a
            # secret in either, including in exception messages.
            raise ValueError("Environment contains a disallowed key or invalid value")
        result[key] = value
    return result


def safe_child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return a runtime-only environment; do not inherit credentials or dotenv.

    Only explicit PYTHONPATH and internal REN_MONITOR_* overrides are accepted.
    Those overrides are trusted backend settings, never an HTTP/UI capability.
    Values are never printed, and forced safety settings cannot be overridden.
    """
    additions = _validate_env(extra or {}, extras_only=True)
    env = {key: value for key in _RUNTIME_ENV if (value := os.environ.get(key)) is not None}
    env.update(additions)
    env.update(_FORCED_ENV)
    return env


def _redact(text: str) -> str:
    text = _AUTH_FIELD.sub(r"\1[REDACTED]", text)
    return _SECRET_FIELD.sub(r"\1[REDACTED]", text)


class _PlainText:
    """Small, constant-memory streaming ANSI/control filter, including OSC/DCS."""

    def __init__(self) -> None:
        self.state = "text"
        self.after_cr = False

    def feed(self, text: str) -> str:
        result: list[str] = []
        for char in text:
            code = ord(char)
            if self.state == "escape":
                if char == "[":
                    self.state = "csi"
                elif char in "]P^_X":
                    self.state = "string"
                elif 0x20 <= code <= 0x2F:
                    self.state = "intermediate"
                else:
                    self.state = "text"
                continue
            if self.state == "intermediate":
                if not 0x20 <= code <= 0x2F:
                    self.state = "text"
                continue
            if self.state == "csi":
                if 0x40 <= code <= 0x7E:
                    self.state = "text"
                elif char == "\x1b":
                    self.state = "escape"
                continue
            if self.state == "string":
                if char in "\x07\x9c":
                    self.state = "text"
                elif char == "\x1b":
                    self.state = "string_escape"
                continue
            if self.state == "string_escape":
                if char == "\\" or char in "\x07\x9c":
                    self.state = "text"
                elif char != "\x1b":
                    self.state = "string"
                continue
            if char == "\x1b":
                self.state = "escape"
            elif char == "\x9b":
                self.state = "csi"
            elif char in "\x90\x9d\x9e\x9f":
                self.state = "string"
            elif char == "\r":
                result.append("\n")
                self.after_cr = True
            elif char == "\n":
                if not self.after_cr:
                    result.append("\n")
                self.after_cr = False
            elif char == "\t":
                result.append(" ")
                self.after_cr = False
            elif code >= 0x20 and not 0x7F <= code <= 0x9F:
                result.append(char)
                self.after_cr = False
        return "".join(result)


class _LogBuffer:
    """Bounded prefix of each line plus a bounded tail of complete lines.

    All methods are called with the monitor lock held. Counts are since clear();
    total_lines counts complete logical lines (including diagnostics and a final
    unterminated line), dropped_lines counts complete lines evicted for capacity.
    A partial line occupies a retention slot but is not yet a completed line.
    """

    def __init__(self, max_lines: int, max_chars: int, max_line_chars: int) -> None:
        self.max_lines = max_lines
        self.max_chars = max_chars
        self.line_limit = min(max_line_chars, max_chars)
        self.lines: deque[str] = deque()
        self.size = 0
        self.partial = ""
        self.partial_overflow = False
        self.total_lines = 0
        self.dropped_lines = 0
        self.truncated = False

    def clear(self) -> None:
        self.lines.clear()
        self.size = 0
        self.partial = ""
        self.partial_overflow = False
        self.total_lines = 0
        self.dropped_lines = 0
        self.truncated = False

    def _render(self, text: str, overflow: bool, *, newline: bool = False) -> str:
        text = _redact(text)
        budget = min(self.line_limit, self.max_chars - int(newline))
        if overflow or len(text) > budget:
            self.truncated = True
            marker = _TRUNCATION[:budget]
            text = text[: max(0, budget - len(marker))] + marker
        return text + ("\n" if newline else "")

    def _trim(self, partial: str = "") -> None:
        slots = int(bool(partial) or self.partial_overflow)
        while self.lines and (
            len(self.lines) + slots > self.max_lines or self.size + len(partial) > self.max_chars
        ):
            self.size -= len(self.lines.popleft())
            self.dropped_lines += 1
            self.truncated = True

    def feed(self, text: str) -> None:
        pieces = text.split("\n")  # text is at most one bounded read, never an unbounded line.
        for index, piece in enumerate(pieces):
            room = self.line_limit - len(self.partial)
            self.partial += piece[:room]
            if len(piece) > room:
                self.partial_overflow = True
                self.truncated = True
            if index != len(pieces) - 1:
                self.finish(newline=True, even_empty=True)
        self._trim(self._render(self.partial, self.partial_overflow))

    def finish(self, *, newline: bool = False, even_empty: bool = False) -> None:
        if not (self.partial or self.partial_overflow or even_empty):
            return
        rendered = self._render(self.partial, self.partial_overflow, newline=newline)
        self.lines.append(rendered)
        self.size += len(rendered)
        self.total_lines += 1
        self.partial = ""
        self.partial_overflow = False
        self._trim()

    def separate(self) -> None:
        """Separate diagnostics/generations from a preceding unterminated line."""
        self.finish(newline=True)
        if self.lines and not self.lines[-1].endswith("\n"):
            last = self.lines.pop()
            self.size -= len(last)
            # A newline is part of the total budget, not an unaccounted separator.
            if len(last) >= self.max_chars:
                last = last[: self.max_chars - 1]
                self.truncated = True
            self.lines.append(last + "\n")
            self.size += len(last) + 1
            self._trim()

    def text(self) -> str:
        partial = self._render(self.partial, self.partial_overflow)
        self._trim(partial)
        return "".join(self.lines) + partial


class OwnedProcessMonitor:
    """Own one POSIX subprocess group and capture combined stdout/stderr.

    Public futures are request receipts: cancelling a receipt does not cancel
    process ownership. Start/stop/clear may be called concurrently from UI or
    HTTP threads. Restart is refused until the previous capture AND group
    cleanup have finished. Logs persist across generations until clear().
    """

    def __init__(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path | None = None,
        env: dict | None = None,
        max_lines: int = 1000,
        max_chars: int = 100000,
        max_line_chars: int = 8192,
        stop_timeout: float = 4.0,
    ) -> None:
        if (
            not isinstance(command, tuple)
            or not command
            or any(not isinstance(arg, str) or "\x00" in arg for arg in command)
            or not command[0]
        ):
            raise ValueError("command must be a nonempty fixed tuple of valid argv strings")
        for name, value in (
            ("max_lines", max_lines),
            ("max_chars", max_chars),
            ("max_line_chars", max_line_chars),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if isinstance(stop_timeout, bool) or not math.isfinite(stop_timeout) or stop_timeout <= 0:
            raise ValueError("stop_timeout must be a finite positive number")
        self._command = command
        self._cwd = Path(cwd) if cwd is not None else None
        self._env = safe_child_env() if env is None else _validate_env(env, extras_only=False)
        self._env.update(_FORCED_ENV)
        self._stop_timeout = float(stop_timeout)
        self._request_budget = self._stop_timeout + _KILL_TIMEOUT + _DRAIN_TIMEOUT + _STARTUP_ALLOWANCE
        self._close_budget = self._request_budget + 2.0
        self._lock = threading.RLock()
        self._buffer = _LogBuffer(max_lines, max_chars, max_line_chars)
        self._state = "idle"
        self._pid: int | None = None
        self._returncode: int | None = None
        self._generation = 0
        self._revision = 0
        self._error: str | None = None
        self._closing = False
        self._closed = threading.Event()
        self._ready = threading.Event()
        self._loop = asyncio.new_event_loop()
        self._active: asyncio.Task[None] | None = None
        self._requests: set[asyncio.Task[Any]] = set()
        self._process: asyncio.subprocess.Process | None = None
        self._stop_event: asyncio.Event | None = None
        self._owned_pgid: int | None = None
        self._unresolved = False
        self._thread = threading.Thread(target=self._thread_main, name="ren-process-monitor", daemon=True)
        self._thread.start()
        if not self._ready.wait(2.0):
            raise RuntimeError("Process monitor event loop did not start")
        atexit.register(self.close)

    def _thread_main(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                # Normal shutdown has already awaited ownership and readers.
                # This bound is only for an exceptional, unresponsive task.
                self._loop.run_until_complete(asyncio.wait(pending, timeout=0.5))
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    def _submit(self, operation: str) -> concurrent.futures.Future[bool]:
        result: concurrent.futures.Future[bool] = concurrent.futures.Future()

        def dispatch() -> None:
            task = self._loop.create_task(self._start() if operation == "start" else self._stop())
            self._requests.add(task)

            def completed(done: asyncio.Task[bool]) -> None:
                self._requests.discard(done)
                try:
                    value = done.result()
                except BaseException as exc:
                    if not result.done():
                        with contextlib.suppress(concurrent.futures.InvalidStateError):
                            result.set_exception(exc)
                else:
                    if not result.done():
                        with contextlib.suppress(concurrent.futures.InvalidStateError):
                            result.set_result(value)

            task.add_done_callback(completed)

        with self._lock:
            if self._closing or self._closed.is_set():
                result.set_result(False)
            else:
                try:
                    self._loop.call_soon_threadsafe(dispatch)
                except RuntimeError:
                    self._failure("Monitor event loop is unavailable")
                    self._state_change("failed")
                    result.set_result(False)
        return result

    def start(self) -> concurrent.futures.Future[bool]:
        """Request startup; False means already active, closed, or spawn failed."""
        return self._submit("start")

    def stop(self) -> concurrent.futures.Future[bool]:
        """Request bounded group cleanup; False means no new stop was requested."""
        return self._submit("stop")

    def snapshot(self) -> dict:
        with self._lock:
            text = self._buffer.text()
            return {
                "state": self._state,
                "pid": self._pid,
                "returncode": self._returncode,
                "generation": self._generation,
                "text": text,
                "total_lines": self._buffer.total_lines,
                "dropped_lines": self._buffer.dropped_lines,
                "revision": self._revision,
                "truncated": self._buffer.truncated,
                "error": self._error,
            }

    def clear(self) -> None:
        """Clear retained display/counters, without resetting the UTF-8 decoder."""
        with self._lock:
            self._buffer.clear()
            self._revision += 1

    def _state_change(self, state: str) -> None:
        with self._lock:
            self._state = state
            self._revision += 1

    def _failure(self, message: str) -> None:
        with self._lock:
            if self._error:
                if message in self._error:
                    return
                self._error = f"{self._error}; {message}"[:2048]
            else:
                self._error = message
            self._buffer.separate()
            self._buffer.feed(f"[monitor] {message}\n")
            self._revision += 1

    def _note(self, message: str) -> None:
        with self._lock:
            self._buffer.separate()
            self._buffer.feed(f"[monitor] {message}\n")
            self._revision += 1

    async def _start(self) -> bool:
        with self._lock:
            if self._closing or self._unresolved or (self._active is not None and not self._active.done()):
                return False
            self._generation += 1
            generation = self._generation
            self._pid = None
            self._returncode = None
            self._error = None
            self._buffer.separate()
            self._state = "starting"
            self._revision += 1
        self._stop_event = asyncio.Event()
        started: asyncio.Future[bool] = self._loop.create_future()
        self._active = self._loop.create_task(self._run(generation, self._stop_event, started))
        return await asyncio.shield(started)

    async def _stop(self) -> bool:
        active = self._active
        event = self._stop_event
        if active is None or active.done() or event is None:
            return False
        # Do not relabel an already observed nonzero natural exit as a requested
        # stop while its descendant/pipe cleanup is still in progress.
        requested = not event.is_set() and (self._process is None or self._process.returncode is None)
        if requested:
            event.set()
            self._state_change("stopping")
        done, _ = await asyncio.wait({active}, timeout=self._request_budget)
        if not done:
            self._failure("Stop exceeded its cleanup deadline; subprocess ownership could not be confirmed")
            self._unresolved = True
            self._emergency_kill()
            active.cancel()
            await asyncio.wait({active}, timeout=_KILL_TIMEOUT + _DRAIN_TIMEOUT)
            self._state_change("failed")
        return requested

    async def _run(self, generation: int, event: asyncio.Event, started: asyncio.Future[bool]) -> None:
        process: asyncio.subprocess.Process | None = None
        reader: asyncio.Task[None] | None = None
        requested = False
        try:
            if event.is_set() or self._closing:
                requested = True
                return
            if os.name != "posix":
                raise RuntimeError("POSIX process-group ownership is required")
            process = await asyncio.create_subprocess_exec(
                *self._command,
                cwd=self._cwd,
                env=self._env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                limit=_STREAM_LIMIT,
            )
            self._process = process
            with self._lock:
                self._owned_pgid = process.pid
                self._pid = process.pid
                self._revision += 1
            assert process.stdout is not None
            reader = self._loop.create_task(self._capture(process.stdout, generation))
            requested = event.is_set() or self._closing
            self._state_change("stopping" if requested else "running")
            started.set_result(not requested)
            # Process.wait() may wait for inherited pipe EOF even after the
            # direct child has exited. Poll the reaped returncode independently.
            while process.returncode is None and not event.is_set() and not self._closing:
                if reader.done() and not reader.cancelled() and reader.exception() is not None:
                    self._failure(f"Output capture failed ({type(reader.exception()).__name__})")
                    break
                await asyncio.sleep(_POLL_INTERVAL)
            requested = requested or event.is_set() or (self._closing and process.returncode is None)
        except asyncio.CancelledError:
            self._failure("Lifecycle was interrupted before normal cleanup completed")
            if process is None:
                self._unresolved = True
        except Exception as exc:
            if os.name != "posix":
                self._failure("POSIX process-group ownership is required; no child was started")
            else:
                errno = f", errno {exc.errno}" if isinstance(exc, OSError) and exc.errno is not None else ""
                self._failure(f"Could not start or supervise owned process ({type(exc).__name__}{errno})")
        finally:
            if not started.done():
                started.set_result(False)
            if process is not None:
                self._state_change("stopping")
                try:
                    await self._cleanup(process, reader)
                except Exception as exc:
                    self._unresolved = True
                    self._failure(f"Owned process cleanup failed ({type(exc).__name__})")
                    self._emergency_kill()
                with self._lock:
                    self._returncode = process.returncode
                if process.returncode is not None and process.returncode != 0 and not requested:
                    self._failure(f"Owned process exited with return code {process.returncode}")
            with self._lock:
                if generation == self._generation:
                    self._pid = None if not self._unresolved else self._pid
                    self._state = "failed" if self._error else "exited"
                    self._revision += 1
                if not self._unresolved:
                    self._owned_pgid = None
            self._process = None

    async def _capture(self, stream: asyncio.StreamReader, generation: int) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        plain = _PlainText()
        try:
            while True:
                chunk = await stream.read(_READ_CHUNK)
                if not chunk:
                    break
                text = plain.feed(decoder.decode(chunk, final=False))
                with self._lock:
                    if generation == self._generation and text:
                        self._buffer.feed(text)
                        self._revision += 1
                # Buffered read() can complete synchronously under a flood.
                # Explicitly yield so stop requests always get loop time.
                await asyncio.sleep(0)
        finally:
            final = plain.feed(decoder.decode(b"", final=True))
            with self._lock:
                if generation == self._generation:
                    self._buffer.feed(final)
                    self._buffer.finish()
                    self._revision += 1

    def _signal_group(self, sig: int) -> None:
        # The only source for this identifier is create_subprocess_exec's owned
        # new-session child. No caller PID or system-wide process-name matching.
        with self._lock:
            pgid = self._owned_pgid
        if pgid is not None:
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                self._release_group(pgid)

    def _release_group(self, pgid: int) -> None:
        # Once observed absent/dead, never signal this numeric PGID again. It
        # could eventually be reused; delayed pipe draining grants no new lease.
        with self._lock:
            if self._owned_pgid == pgid:
                self._owned_pgid = None

    def _group_alive(self) -> bool:
        with self._lock:
            pgid = self._owned_pgid
        if pgid is None:
            return False
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            self._release_group(pgid)
            return False
        if not sys.platform.startswith("linux"):
            return True
        # Linux container PID 1 is not always a reaper. killpg(..., 0) counts
        # zombies as existing indefinitely. Read only public stat metadata to
        # check the *owned group*, never memory, environ, cmdline, or credentials.
        matched_dead = False
        try:
            with os.scandir("/proc") as entries:
                for entry in entries:
                    if not entry.name.isdecimal():
                        continue
                    try:
                        with open(f"{entry.path}/stat", encoding="ascii", errors="replace") as stat:
                            value = stat.read(4096)
                        fields = value[value.rfind(")") + 2 :].split()
                        if len(fields) >= 3 and int(fields[2]) == pgid:
                            if fields[0] not in {"Z", "X"}:
                                return True
                            matched_dead = True
                    except (OSError, ValueError):
                        continue
        except OSError:
            return True
        if matched_dead:
            self._release_group(pgid)
            return False
        # The group may have disappeared during the scan. If membership could
        # not be established, be conservative rather than claiming cleanup.
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            self._release_group(pgid)
            return False

    async def _cleanup(self, process: asyncio.subprocess.Process, reader: asyncio.Task[None] | None) -> None:
        try:
            if self._group_alive():
                self._signal_group(signal.SIGTERM)
                deadline = self._loop.time() + self._stop_timeout
                while self._group_alive() and self._loop.time() < deadline:
                    await asyncio.sleep(_POLL_INTERVAL)
                if self._group_alive():
                    self._note("Grace period expired; sending SIGKILL to the remaining owned process group")
                    self._signal_group(signal.SIGKILL)
            deadline = self._loop.time() + _KILL_TIMEOUT
            while (process.returncode is None or self._group_alive()) and self._loop.time() < deadline:
                await asyncio.sleep(_POLL_INTERVAL)
            if process.returncode is None or self._group_alive():
                self._unresolved = True
                self._failure("Owned process group did not finish after SIGKILL")
        except OSError as exc:
            self._unresolved = True
            self._failure(f"Owned process-group cleanup could not be confirmed ({type(exc).__name__})")
            self._emergency_kill()
            deadline = self._loop.time() + _KILL_TIMEOUT
            while process.returncode is None and self._loop.time() < deadline:
                await asyncio.sleep(_POLL_INTERVAL)
        finally:
            # Always finish/close the reader, even when signaling itself fails.
            try:
                if reader is not None:
                    done, _ = await asyncio.wait({reader}, timeout=_DRAIN_TIMEOUT)
                    if not done:
                        self._failure(
                            "Output pipe did not reach EOF after owned-group cleanup; capture was closed"
                        )
                        reader.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        try:
                            await reader
                        except Exception as exc:
                            self._failure(f"Output capture failed ({type(exc).__name__})")
            finally:
                # A deliberately escaped descendant can retain a pipe. Do not
                # attach to/kill it or wait forever. asyncio has no public
                # pipe-close method on Process/StreamReader.
                if process.stdout is not None:
                    transport = getattr(process.stdout, "_transport", None)
                    if transport is not None:
                        transport.close()
            if process.returncode is not None:
                await process.wait()  # immediately available once returncode is set

    def _emergency_kill(self) -> None:
        if os.name == "posix":
            try:
                self._signal_group(signal.SIGKILL)
            except OSError as exc:
                self._failure(f"Emergency owned-group kill failed ({type(exc).__name__})")

    async def _shutdown(self) -> None:
        await self._stop()
        current = asyncio.current_task()
        pending = {task for task in self._requests if task is not current and not task.done()}
        if pending:
            done, remaining = await asyncio.wait(pending, timeout=0.5)
            for task in remaining:
                task.cancel()
            if remaining:
                await asyncio.gather(*remaining, return_exceptions=True)

    def close(self) -> None:
        """Bounded, idempotent stop/drain/join; also registered with atexit."""
        if threading.current_thread() is self._thread:
            raise RuntimeError("close() must be called outside the monitor loop thread")
        with self._lock:
            if self._closed.is_set():
                return
            first = not self._closing
            self._closing = True
        if not first:
            self._closed.wait(self._close_budget + 2.0)
            return
        try:
            shutdown = self._shutdown()
            try:
                future = asyncio.run_coroutine_threadsafe(shutdown, self._loop)
            except RuntimeError:
                shutdown.close()
                self._unresolved = True
                self._failure("Shutdown could not use the monitor event loop")
                self._emergency_kill()
            else:
                try:
                    future.result(timeout=self._close_budget)
                except Exception as exc:
                    self._unresolved = True
                    self._failure(f"Shutdown could not complete within its deadline ({type(exc).__name__})")
                    self._emergency_kill()
                    future.cancel()
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=1.0)
            if self._thread.is_alive():
                self._unresolved = True
                self._failure("Monitor event-loop thread did not stop within its deadline")
        finally:
            with self._lock:
                self._state = "failed" if self._unresolved else "closed"
                if not self._unresolved:
                    self._pid = None
                self._revision += 1
            self._closed.set()
            atexit.unregister(self.close)
