"""Synthetic, local-only subprocess fixtures for the stdout monitor contract."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import ctypes
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from workbench_pilot import process_monitor as implementation
from workbench_pilot.process_monitor import OwnedProcessMonitor, safe_child_env

pytestmark = pytest.mark.monitor


@pytest.fixture
def monitors():
    owned: list[OwnedProcessMonitor] = []

    def make(code: str, **kwargs) -> OwnedProcessMonitor:
        kwargs.setdefault("stop_timeout", 0.15)
        monitor = OwnedProcessMonitor((sys.executable, "-u", "-c", code), **kwargs)
        owned.append(monitor)
        return monitor

    yield make
    for monitor in reversed(owned):
        monitor.close()
        assert not monitor._thread.is_alive()


def wait_for(monitor: OwnedProcessMonitor, predicate, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = monitor.snapshot()
        if predicate(snapshot):
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"Monitor did not reach the expected condition: {monitor.snapshot()!r}")


def finished(monitor: OwnedProcessMonitor, timeout: float = 5.0) -> dict:
    return wait_for(monitor, lambda value: value["state"] in {"exited", "failed"}, timeout)


def executing(pid: int) -> bool:
    """Test-owned PID only; zombies are terminated, not executing children."""
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            return stat[stat.rfind(")") + 2 :].split()[0] not in {"Z", "X"}
        except FileNotFoundError:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_dead(pid: int) -> None:
    deadline = time.monotonic() + 3.0
    while executing(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not executing(pid), "An owned fixture process is still executing"


@contextlib.contextmanager
def fixture_reaper():
    """Reap only our fixture descendants on Linux containers with a non-reaping PID 1.

    This is test-harness policy, NOT a change made by the monitor library. Restore
    the previous subreaper setting and never waitpid/reap an unrelated child.
    """
    descendants: list[int] = []
    libc = None
    previous = ctypes.c_int()
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0  # PR_GET_CHILD_SUBREAPER
        assert libc.prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
    try:
        yield descendants
    finally:
        for pid in descendants:
            if executing(pid):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            if libc is not None:
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    try:
                        reaped, _ = os.waitpid(pid, os.WNOHANG)
                    except ChildProcessError:
                        break
                    if reaped:
                        break
                    time.sleep(0.01)
        if libc is not None:
            assert libc.prctl(36, previous.value, 0, 0, 0) == 0


def test_safe_environment_whitelist_and_forced_settings(monkeypatch):
    # Replace, do not enumerate or log, the real parent environment.
    synthetic = {
        "PATH": "/synthetic/bin",
        "HOME": "/synthetic/home",
        "TMPDIR": "/synthetic/tmp",
        "LANG": "C.UTF-8",
        "DISPLAY": ":99",
        "OPENAI_API_KEY": "synthetic-provider-secret",
        "ANTHROPIC_API_KEY": "synthetic-provider-secret",
        "AWS_SESSION_TOKEN": "synthetic-cloud-token",
        "SSH_AUTH_SOCK": "/synthetic/socket",
        "LD_PRELOAD": "/synthetic/unsafe.so",
        "BASH_ENV": "/synthetic/.env",
        "ENV": "/synthetic/.env",
        "UV_ENV_FILE": "/synthetic/.env",
        "PYTHONPATH": "/synthetic/untrusted",
        "PYTHONSTARTUP": "/synthetic/startup.py",
        "REN_MONITOR_TOKEN": "synthetic-not-implicitly-inherited",
        "PYTHONUNBUFFERED": "0",
        "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "true",
    }
    monkeypatch.setattr(os, "environ", synthetic)
    result = safe_child_env({"REN_MONITOR_TEST": "synthetic", "PYTHONPATH": "/synthetic/source"})
    assert result == {
        "PATH": "/synthetic/bin",
        "HOME": "/synthetic/home",
        "TMPDIR": "/synthetic/tmp",
        "LANG": "C.UTF-8",
        "DISPLAY": ":99",
        "REN_MONITOR_TEST": "synthetic",
        "PYTHONPATH": "/synthetic/source",
        "PYTHONUNBUFFERED": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHON_DOTENV_DISABLED": "1",
        "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
    }


@pytest.mark.parametrize(
    "key",
    [
        "OPENAI_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "HOME",
        "PATH",
        "BASH_ENV",
        "LD_PRELOAD",
        "PYTHONSTARTUP",
        "STREAMLIT_BROWSER_GATHER_USAGE_STATS",
        "REN_MONITOR_",
        "REN_MONITOR_a",
    ],
)
def test_environment_extra_cannot_override_runtime_or_add_secrets(key):
    with pytest.raises(ValueError, match="disallowed"):
        safe_child_env({key: "synthetic-value-not-to-be-echoed"})


@pytest.mark.parametrize(
    "env",
    [
        {"OPENAI_API_KEY": "synthetic-secret"},
        {"SSH_AUTH_SOCK": "/synthetic/socket"},
        {"PYTHONPATH": "bad\x00path"},
    ],
)
def test_constructor_also_validates_explicit_env(env):
    with pytest.raises(ValueError, match="disallowed") as caught:
        OwnedProcessMonitor((sys.executable, "-c", "pass"), env=env)
    assert "synthetic-secret" not in str(caught.value)


def test_environment_rejects_non_string_values():
    with pytest.raises(TypeError):
        safe_child_env({"REN_MONITOR_TEST": 123})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_lines": 0},
        {"max_lines": True},
        {"max_chars": -1},
        {"max_line_chars": 1.5},
        {"stop_timeout": 0},
        {"stop_timeout": float("nan")},
        {"stop_timeout": float("inf")},
    ],
)
def test_invalid_limits_rejected(kwargs):
    with pytest.raises(ValueError):
        OwnedProcessMonitor((sys.executable, "-c", "pass"), **kwargs)


@pytest.mark.parametrize("command", [(), [], ("",), (sys.executable, "bad\x00argument")])
def test_fixed_valid_argv_required(command):
    with pytest.raises(ValueError):
        OwnedProcessMonitor(command)


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_initial_snapshot_no_automatic_spawn_and_closed_requests(monitors):
    monitor = monitors("raise AssertionError('must not be spawned')")
    snapshot = monitor.snapshot()
    assert snapshot == {
        "state": "idle",
        "pid": None,
        "returncode": None,
        "generation": 0,
        "text": "",
        "total_lines": 0,
        "dropped_lines": 0,
        "revision": 0,
        "truncated": False,
        "error": None,
    }
    assert monitor._thread.is_alive()
    assert monitor.stop().result(timeout=1) is False
    monitor.close()
    monitor.close()
    assert monitor.snapshot()["state"] == "closed"
    assert monitor.start().result(timeout=1) is False
    assert monitor.stop().result(timeout=1) is False
    assert not monitor._thread.is_alive()


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_real_async_exec_combines_stdout_stderr_and_uses_no_shell(monitors, monkeypatch, tmp_path):
    actual = asyncio.create_subprocess_exec
    calls = []

    async def observe(*args, **kwargs):
        # Record settings, not environment values.
        calls.append((args, {key: value for key, value in kwargs.items() if key != "env"}))
        return await actual(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe)
    monitor = monitors(
        "import os, pathlib\n"
        "assert 'OPENAI_API_KEY' not in os.environ\n"
        "assert os.environ['PYTHONUNBUFFERED'] == '1'\n"
        "assert os.environ['PYTHON_DOTENV_DISABLED'] == '1'\n"
        "assert os.environ['STREAMLIT_BROWSER_GATHER_USAGE_STATS'] == 'false'\n"
        "assert os.read(0, 1) == b''\n"
        "pathlib.Path('owned-marker').write_text('synthetic')\n"
        "os.write(1, b'out-one\\n')\n"
        "os.write(2, b'err-one\\n')\n"
        "os.write(1, b'last unterminated')\n",
        cwd=tmp_path,
        env={"PYTHONUNBUFFERED": "0"},
    )
    assert monitor.start().result(timeout=2)
    snapshot = finished(monitor)
    assert snapshot["state"] == "exited"
    assert snapshot["returncode"] == 0
    assert snapshot["pid"] is None
    assert snapshot["text"] == "out-one\nerr-one\nlast unterminated"
    assert snapshot["total_lines"] == 3
    assert snapshot["error"] is None
    assert (tmp_path / "owned-marker").read_text() == "synthetic"
    assert len(calls) == 1
    args, options = calls[0]
    assert args[0] == sys.executable
    assert options["stdout"] == asyncio.subprocess.PIPE
    assert options["stderr"] == asyncio.subprocess.STDOUT
    assert options["stdin"] == asyncio.subprocess.DEVNULL
    assert options["start_new_session"] is True
    assert "shell" not in options
    assert options["limit"] <= 65536


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_concurrent_start_is_idempotent_and_concurrent_stop_is_bounded(monitors):
    monitor = monitors("import time; print('ready', flush=True); time.sleep(60)")
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        starts = list(pool.map(lambda _: monitor.start(), range(24)))
        assert sum(future.result(timeout=2) for future in starts) == 1
        wait_for(monitor, lambda snapshot: "ready" in snapshot["text"])
        pid = monitor.snapshot()["pid"]
        assert monitor.snapshot()["generation"] == 1
        stops = list(pool.map(lambda _: monitor.stop(), range(24)))
        assert sum(future.result(timeout=4) for future in stops) == 1
    snapshot = finished(monitor)
    assert snapshot["state"] == "exited"
    assert snapshot["error"] is None
    assert snapshot["returncode"] == -signal.SIGTERM
    wait_dead(pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_restarts_wait_for_previous_generation_and_keep_logs(monitors, tmp_path):
    count = tmp_path / "generation"
    monitor = monitors(
        f"from pathlib import Path\np=Path({str(count)!r})\n"
        "n=int(p.read_text())+1 if p.exists() else 1\n"
        "p.write_text(str(n))\nprint('generation-' + str(n), end='', flush=True)\n"
    )
    for generation in range(1, 5):
        assert monitor.start().result(timeout=2)
        snapshot = finished(monitor)
        assert snapshot["generation"] == generation
        assert snapshot["returncode"] == 0
        assert snapshot["text"].endswith(f"generation-{generation}")
    assert monitor.snapshot()["text"] == "generation-1\ngeneration-2\ngeneration-3\ngeneration-4"
    assert count.read_text() == "4"


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_partial_unicode_survives_every_byte_boundary_and_flushes(monitors, tmp_path):
    release = tmp_path / "release"
    payload = "进度 café 🧵 日本語"
    monitor = monitors(
        "import os, time\nfrom pathlib import Path\n"
        f"data={payload.encode('utf-8')!r}\n"
        "for byte in data:\n os.write(1, bytes([byte])); time.sleep(.002)\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(.01)\n"
        "os.write(1, b'\\nfinal \\xe2\\x82')\n"
    )
    assert monitor.start().result(timeout=2)
    partial = wait_for(monitor, lambda snapshot: snapshot["text"] == payload)
    assert partial["state"] == "running"
    assert partial["total_lines"] == 0
    assert "�" not in partial["text"]
    release.touch()
    snapshot = finished(monitor)
    assert snapshot["text"] == payload + "\nfinal �"
    assert snapshot["total_lines"] == 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_ansi_control_and_obvious_secret_fields_redacted_across_chunks(monitors, tmp_path):
    release = tmp_path / "release"
    chunks = [
        b"\\x1b[".decode("unicode_escape").encode(),
        b"31mred\x1b[0m ",
        b"\x1b]8;;https://invalid.example/synthetic",
        b"\x1b\\link\x1b]8;;\x07\n",
        b"\x1bPignored synthetic control content\x1b\\clean\x00\x08\x7f\n",
        b"Authorization: Bea",
        b"rer synthetic-auth\n",
        b'X-API-Key: synthetic-key\n{"pass',
        b'word": "synthetic password"}\n',
        b"token=synthetic-token; ok\npartial password=synthetic-",
        b"partial",
    ]
    monitor = monitors(
        "import os, time\nfrom pathlib import Path\n"
        f"chunks={chunks!r}\n"
        "for chunk in chunks:\n os.write(1, chunk); time.sleep(.01)\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(.01)\n"
    )
    assert monitor.start().result(timeout=2)
    snapshot = wait_for(monitor, lambda value: "partial password=[REDACTED]" in value["text"])
    assert "red link\nclean\n" in snapshot["text"]
    for secret in ["synthetic-auth", "synthetic-key", "synthetic password", "synthetic-token", "synthetic-"]:
        assert secret not in snapshot["text"]
    assert not any(ord(char) < 32 and char != "\n" for char in snapshot["text"])
    release.touch()
    assert finished(monitor)["text"].endswith("partial password=[REDACTED]")


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_crlf_and_carriage_return_progress_are_normalized(monitors):
    monitor = monitors("import os; os.write(1,b'one\\r\\ntwo\\rthree\\tend\\n')")
    assert monitor.start().result(timeout=2)
    assert finished(monitor)["text"] == "one\ntwo\nthree end\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
@pytest.mark.parametrize("max_chars,max_line_chars", [(90, 30), (8, 20), (1, 1)])
def test_unbounded_line_has_bounded_partial_and_final_snapshot(monitors, tmp_path, max_chars, max_line_chars):
    release = tmp_path / "release"
    monitor = monitors(
        "import os, time\nfrom pathlib import Path\n"
        "os.write(1, b'x' * 2_000_000)\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(.01)\n",
        max_lines=2,
        max_chars=max_chars,
        max_line_chars=max_line_chars,
    )
    assert monitor.start().result(timeout=2)
    snapshot = wait_for(monitor, lambda value: value["truncated"])
    assert len(snapshot["text"]) <= min(max_chars, max_line_chars)
    assert len(monitor._buffer.partial) <= min(max_chars, max_line_chars)
    release.touch()
    snapshot = finished(monitor)
    assert snapshot["state"] == "exited"
    assert snapshot["total_lines"] == 1
    assert snapshot["truncated"]
    assert len(snapshot["text"]) <= min(max_chars, max_line_chars)


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_flood_and_simultaneous_stdout_stderr_keep_only_bounded_tail(monitors):
    monitor = monitors(
        "import os, threading\n"
        "def emit(fd, prefix):\n"
        " for i in range(6000): os.write(fd, (prefix+str(i)+'\\n').encode())\n"
        "a=threading.Thread(target=emit,args=(1,'stdout-'))\n"
        "b=threading.Thread(target=emit,args=(2,'stderr-'))\n"
        "a.start(); b.start(); a.join(); b.join()\n"
        "os.write(1,b'FINAL-OUT\\n'); os.write(2,b'FINAL-ERR\\n')\n",
        max_lines=7,
        max_chars=90,
        max_line_chars=25,
    )
    assert monitor.start().result(timeout=2)
    deadline = time.monotonic() + 6
    while True:
        snapshot = monitor.snapshot()
        assert len(snapshot["text"]) <= 90
        assert len(snapshot["text"].splitlines()) <= 7
        assert all(len(line) <= 25 for line in snapshot["text"].splitlines())
        assert monitor._buffer.size <= 90
        if snapshot["state"] in {"exited", "failed"}:
            break
        assert time.monotonic() < deadline
        time.sleep(0.002)
    assert snapshot["state"] == "exited"
    assert snapshot["text"].endswith("FINAL-OUT\nFINAL-ERR\n")
    assert snapshot["total_lines"] == 12002
    assert snapshot["dropped_lines"] >= 11995
    assert snapshot["truncated"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_missing_executable_and_nonzero_exit_are_visible(tmp_path, monitors):
    missing = OwnedProcessMonitor((str(tmp_path / "does-not-exist"),), stop_timeout=0.1)
    try:
        assert missing.start().result(timeout=2) is False
        snapshot = finished(missing)
        assert snapshot["state"] == "failed"
        assert snapshot["pid"] is None
        assert snapshot["returncode"] is None
        assert "FileNotFoundError" in snapshot["error"]
        assert snapshot["error"] in snapshot["text"]
        assert missing.start().result(timeout=2) is False
        assert finished(missing)["generation"] == 2
    finally:
        missing.close()
    monitor = monitors("import os; os.write(2,b'synthetic crash tail'); raise SystemExit(7)")
    assert monitor.start().result(timeout=2)
    snapshot = finished(monitor)
    assert snapshot["state"] == "failed"
    assert snapshot["returncode"] == 7
    assert "return code 7" in snapshot["error"]
    assert "synthetic crash tail" in snapshot["text"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_stop_during_startup_still_cleans_up_eventual_child(monitors, monkeypatch):
    actual = asyncio.create_subprocess_exec
    entered = threading.Event()
    children = []

    async def slow_spawn(*args, **kwargs):
        entered.set()
        await asyncio.sleep(0.15)
        child = await actual(*args, **kwargs)
        children.append(child.pid)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_spawn)
    monitor = monitors("import time; time.sleep(60)")
    start = monitor.start()
    assert entered.wait(2)
    stop = monitor.stop()
    assert start.result(timeout=2) is False
    assert stop.result(timeout=4) is True
    assert children
    assert finished(monitor)["state"] == "exited"
    for pid in children:
        wait_dead(pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_cancelling_start_receipt_does_not_cancel_ownership(monitors, monkeypatch):
    actual = asyncio.create_subprocess_exec
    entered = threading.Event()

    async def slow_spawn(*args, **kwargs):
        entered.set()
        await asyncio.sleep(0.1)
        return await actual(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_spawn)
    monitor = monitors("import time; print('ready'); time.sleep(60)")
    receipt = monitor.start()
    assert entered.wait(2)
    assert receipt.cancel()
    snapshot = wait_for(monitor, lambda value: "ready" in value["text"])
    assert snapshot["state"] == "running"
    pid = snapshot["pid"]
    monitor.close()
    wait_dead(pid)
    assert monitor.snapshot()["state"] == "closed"


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_nonresponsive_startup_has_bounded_stop_and_visible_uncertainty(monitors, monkeypatch):
    entered = threading.Event()

    async def never_spawn(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("fixture never creates a child")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", never_spawn)
    monitor = monitors("pass", stop_timeout=0.05)
    receipt = monitor.start()
    assert entered.wait(2)
    began = time.monotonic()
    assert monitor.stop().result(timeout=6)
    assert time.monotonic() - began < 5.5
    assert receipt.result(timeout=1) is False
    assert monitor.snapshot()["state"] == "failed"
    assert "deadline" in monitor.snapshot()["error"]
    assert monitor.start().result(timeout=1) is False  # uncertain ownership forbids restart


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_stop_escalates_for_term_ignoring_child(monitors):
    monitor = monitors(
        "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\nwhile True: time.sleep(.1)\n",
        stop_timeout=0.1,
    )
    assert monitor.start().result(timeout=2)
    snapshot = wait_for(monitor, lambda value: "ready" in value["text"])
    pid = snapshot["pid"]
    before = time.monotonic()
    assert monitor.stop().result(timeout=4)
    assert time.monotonic() - before < 3
    snapshot = finished(monitor)
    assert snapshot["state"] == "exited"
    assert snapshot["returncode"] == -signal.SIGKILL
    assert snapshot["error"] is None
    assert "SIGKILL" in snapshot["text"]
    wait_dead(pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
@pytest.mark.parametrize("inherit_pipe", [True, False])
def test_naturally_exiting_parent_cannot_leave_running_descendant(monitors, tmp_path, inherit_pipe):
    ready = tmp_path / "descendant-ready"
    child_code = (
        "import signal, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        f"Path({str(ready)!r}).touch()\n"
        "while True: time.sleep(.1)\n"
    )
    pipe_options = "" if inherit_pipe else ", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL"
    code = (
        "import subprocess, sys, time\nfrom pathlib import Path\n"
        f"child=subprocess.Popen([sys.executable,'-u','-c',{child_code!r}]{pipe_options})\n"
        f"while not Path({str(ready)!r}).exists(): time.sleep(.005)\n"
        "print('descendant='+str(child.pid), flush=True)\n"
    )
    with fixture_reaper() as descendants:
        monitor = monitors(code, stop_timeout=0.1)
        assert monitor.start().result(timeout=2)
        snapshot = wait_for(monitor, lambda value: "descendant=" in value["text"])
        descendant = int(re.search(r"descendant=(\d+)", snapshot["text"])[1])
        descendants.append(descendant)
        snapshot = finished(monitor, timeout=4)
        assert snapshot["state"] == "exited"
        assert snapshot["returncode"] == 0  # do not replace direct parent's exit code with SIGKILL
        assert snapshot["error"] is None
        wait_dead(descendant)
        monitor.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_capture_failure_is_visible_and_cleans_up_owned_child(monitors, monkeypatch):
    async def broken_capture(self, stream, generation):
        raise OSError("synthetic read failure")

    monkeypatch.setattr(OwnedProcessMonitor, "_capture", broken_capture)
    monitor = monitors("import time; time.sleep(60)")
    assert monitor.start().result(timeout=2)
    pid = monitor.snapshot()["pid"]
    snapshot = finished(monitor)
    assert snapshot["state"] == "failed"
    assert "Output capture failed" in snapshot["error"]
    assert snapshot["returncode"] is not None
    if pid is not None:
        wait_dead(pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_clear_during_output_does_not_reset_decoder_or_stop_child(monitors, tmp_path):
    release = tmp_path / "release"
    monitor = monitors(
        "import os, time\nfrom pathlib import Path\n"
        "os.write(1,b'before\\nold partial \\xe2')\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(.01)\n"
        "os.write(1,b'\\x82\\xacafter\\n')\n",
    )
    assert monitor.start().result(timeout=2)
    before = wait_for(monitor, lambda value: "old partial" in value["text"])
    monitor.clear()
    cleared = monitor.snapshot()
    assert cleared["text"] == ""
    assert cleared["state"] == "running"
    assert cleared["pid"] == before["pid"]
    assert cleared["generation"] == before["generation"]
    assert cleared["revision"] > before["revision"]
    assert cleared["total_lines"] == cleared["dropped_lines"] == 0
    assert not cleared["truncated"]
    release.touch()
    after = finished(monitor)
    assert after["text"] == "€after\n"
    assert after["total_lines"] == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_snapshot_clear_start_stop_races_do_not_leak_threads_or_processes(monitors):
    monitor = monitors(
        "import os, time\nwhile True:\n os.write(1,b'synthetic-progress\\n'); time.sleep(.001)\n",
        max_lines=4,
        max_chars=80,
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for generation in range(1, 5):
            assert monitor.start().result(timeout=2)
            snapshot = wait_for(monitor, lambda value: "synthetic-progress" in value["text"])
            pid = snapshot["pid"]

            def churn(index):
                if index % 3 == 0:
                    monitor.clear()
                elif index % 3 == 1:
                    assert monitor.start().result(timeout=2) is False
                else:
                    assert len(monitor.snapshot()["text"]) <= 80

            list(pool.map(churn, range(90)))
            assert monitor.stop().result(timeout=4)
            assert finished(monitor)["generation"] == generation
            wait_dead(pid)
        closures = [pool.submit(monitor.close) for _ in range(8)]
        for future in closures:
            future.result(timeout=5)
    assert monitor.snapshot()["state"] == "closed"
    assert not monitor._thread.is_alive()


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_close_during_startup_and_immediate_close_are_safe(monitors, monkeypatch):
    actual = asyncio.create_subprocess_exec
    entered = threading.Event()
    spawned = []

    async def slow_spawn(*args, **kwargs):
        entered.set()
        await asyncio.sleep(0.1)
        process = await actual(*args, **kwargs)
        spawned.append(process.pid)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_spawn)
    monitor = monitors("import time; time.sleep(60)")
    start = monitor.start()
    assert entered.wait(2)
    monitor.close()
    assert start.result(timeout=1) is False
    assert monitor.snapshot()["state"] == "closed"
    for pid in spawned:
        wait_dead(pid)
    for _ in range(8):
        monitor = monitors("import time; time.sleep(60)")
        receipt = monitor.start()
        monitor.close()
        assert receipt.result(timeout=1) is False
        assert monitor.snapshot()["state"] == "closed"


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_ordinary_monitor_parent_exit_runs_atexit_cleanup_for_entire_group(tmp_path):
    marker = tmp_path / "owned-pids"
    ready = tmp_path / "descendant-ready"
    descendant_code = (
        "import signal, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        f"Path({str(ready)!r}).touch()\n"
        "while True: time.sleep(.1)\n"
    )
    child_code = (
        "import os, subprocess, sys, time\nfrom pathlib import Path\n"
        f"p=subprocess.Popen([sys.executable,'-u','-c',{descendant_code!r}])\n"
        f"while not Path({str(ready)!r}).exists(): time.sleep(.005)\n"
        f"Path({str(marker)!r}).write_text(str(os.getpid())+' '+str(p.pid))\n"
        "while True: time.sleep(.1)\n"
    )
    source = str(Path(implementation.__file__).resolve().parents[1])
    parent_code = (
        "import sys, time\nfrom pathlib import Path\n"
        "from workbench_pilot.process_monitor import OwnedProcessMonitor\n"
        f"m=OwnedProcessMonitor((sys.executable,'-u','-c',{child_code!r}),stop_timeout=.1)\n"
        "assert m.start().result(timeout=3)\n"
        f"while not Path({str(marker)!r}).exists(): time.sleep(.01)\n"
        "# Deliberately omit close: ordinary interpreter exit must run atexit.\n"
    )
    with fixture_reaper() as descendants:
        outer = subprocess.Popen(
            [sys.executable, "-u", "-c", parent_code],
            env=safe_child_env({"PYTHONPATH": source}),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            output, _ = outer.communicate(timeout=8)
            assert outer.returncode == 0, output.decode(errors="replace")
            assert marker.exists()
            pids = [int(part) for part in marker.read_text().split()]
            descendants.extend(pids)
            for pid in pids:
                wait_dead(pid)
        finally:
            if outer.poll() is None:
                os.killpg(outer.pid, signal.SIGKILL)
                outer.wait(timeout=2)
            if marker.exists():
                pids = [int(part) for part in marker.read_text().split()]
                for pid in pids:
                    if pid not in descendants:
                        descendants.append(pid)
                    if executing(pid):
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(pid, signal.SIGKILL)


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_infinite_pipe_flood_does_not_starve_stop_or_snapshot(monitors):
    monitor = monitors(
        "import os\nos.write(1,b'ready\\n')\nwhile True: os.write(1,b'x'*65536)\n",
        max_lines=2,
        max_chars=64,
        max_line_chars=32,
        stop_timeout=0.1,
    )
    assert monitor.start().result(timeout=2)
    snapshot = wait_for(monitor, lambda value: value["truncated"])
    pid = snapshot["pid"]
    assert len(snapshot["text"]) <= 64
    assert len(monitor._buffer.partial) <= 32
    began = time.monotonic()
    receipt = monitor.stop()
    assert time.monotonic() - began < 0.2  # public call is never the cleanup wait
    assert receipt.result(timeout=4)
    assert time.monotonic() - began < 3
    assert finished(monitor)["state"] == "exited"
    wait_dead(pid)


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_restart_after_nonzero_exit_resets_status_not_prior_logs(monitors, tmp_path):
    flag = tmp_path / "first-failure"
    monitor = monitors(
        "from pathlib import Path\n"
        f"p=Path({str(flag)!r})\n"
        "if not p.exists():\n p.touch(); print('first failure'); raise SystemExit(9)\n"
        "print('second success')\n"
    )
    assert monitor.start().result(timeout=2)
    assert finished(monitor)["returncode"] == 9
    assert monitor.start().result(timeout=2)
    snapshot = finished(monitor)
    assert snapshot["state"] == "exited"
    assert snapshot["returncode"] == 0
    assert snapshot["error"] is None
    assert snapshot["generation"] == 2
    assert "first failure" in snapshot["text"]
    assert snapshot["text"].endswith("second success\n")


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_start_refused_during_descendant_cleanup_and_late_stop_cannot_hide_crash(monitors, tmp_path):
    ready = tmp_path / "ready"
    descendant_code = (
        "import signal, time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        f"Path({str(ready)!r}).touch()\n"
        "while True: time.sleep(.1)\n"
    )
    code = (
        "import subprocess, sys, time\nfrom pathlib import Path\n"
        f"p=subprocess.Popen([sys.executable,'-u','-c',{descendant_code!r}])\n"
        "print('descendant='+str(p.pid),flush=True)\n"
        f"while not Path({str(ready)!r}).exists(): time.sleep(.005)\n"
        "raise SystemExit(11)\n"
    )
    with fixture_reaper() as descendants:
        monitor = monitors(code, stop_timeout=0.4)
        assert monitor.start().result(timeout=2)
        snapshot = wait_for(monitor, lambda value: "descendant=" in value["text"])
        pid = int(re.search(r"descendant=(\d+)", snapshot["text"])[1])
        descendants.append(pid)
        wait_for(monitor, lambda value: value["state"] == "stopping")
        assert monitor.start().result(timeout=1) is False
        assert monitor.stop().result(timeout=3) is False
        snapshot = finished(monitor)
        assert snapshot["state"] == "failed"
        assert snapshot["generation"] == 1
        assert snapshot["returncode"] == 11
        assert "return code 11" in snapshot["error"]
        wait_dead(pid)
        monitor.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_cleanup_signal_failure_is_reported_not_fake_success(monitors, monkeypatch):
    monitor = monitors("import time; print('ready',flush=True); time.sleep(60)")
    assert monitor.start().result(timeout=2)
    pid = wait_for(monitor, lambda value: "ready" in value["text"])["pid"]
    actual = monitor._signal_group

    def fail_term(sig):
        if sig == signal.SIGTERM:
            raise PermissionError("synthetic permission failure")
        return actual(sig)

    monkeypatch.setattr(monitor, "_signal_group", fail_term)
    assert monitor.stop().result(timeout=4)
    snapshot = finished(monitor)
    assert snapshot["state"] == "failed"
    assert "PermissionError" in snapshot["error"]
    assert "PermissionError" in snapshot["text"]
    assert snapshot["returncode"] == -signal.SIGKILL
    wait_dead(pid)
    monitor.close()
    assert monitor.snapshot()["state"] == "failed"  # uncertainty is not a successful shutdown


@pytest.mark.skipif(os.name != "posix", reason="POSIX-owned process group contract")
def test_argv_is_literal_and_child_is_in_new_owned_group(tmp_path):
    marker = tmp_path / "must-not-be-created"
    literal = f"; touch {marker} $(touch {marker})"
    monitor = OwnedProcessMonitor(
        (
            sys.executable,
            "-u",
            "-c",
            "import sys,time; print(sys.argv[1],flush=True); time.sleep(60)",
            literal,
        ),
        stop_timeout=0.1,
    )
    try:
        assert monitor.start().result(timeout=2)
        snapshot = wait_for(monitor, lambda value: literal in value["text"])
        pid = snapshot["pid"]
        assert os.getpgid(pid) == pid
        assert os.getpgid(pid) != os.getpgrp()
        assert not marker.exists()
        monitor.close()
        wait_dead(pid)
    finally:
        monitor.close()
