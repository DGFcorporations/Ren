"""Actual web-mode CLI launch and SIGTERM cleanup of its owned local services."""

from __future__ import annotations

import importlib.util
import re
import socket
import sys
import time
from pathlib import Path

import httpx
import pytest

from workbench_pilot.process_monitor import OwnedProcessMonitor, safe_child_env

pytestmark = pytest.mark.monitor


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("CLI service condition timed out")


def closed(port_number):
    try:
        with socket.create_connection(("127.0.0.1", port_number), timeout=0.2):
            return False
    except OSError:
        return True


def owned_descendants(root_pid):
    found, todo = set(), [root_pid]
    while todo:
        parent = todo.pop()
        children = []
        # Async subprocesses may be children of a worker TID, not the main TID.
        for path in Path(f"/proc/{parent}/task").glob("*/children"):
            try:
                children.extend(int(p) for p in path.read_text().split())
            except (OSError, ValueError):
                pass
        for child in children:
            if child not in found:
                found.add(child)
                todo.append(child)
    return found


def not_executing(pid):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        return raw[raw.rfind(")") + 2 :].split()[0] in {"Z", "X", "x"}
    except (OSError, IndexError):
        return True


def test_actual_web_cli_shutdown_closes_only_its_owned_services(tmp_path):
    if importlib.util.find_spec("streamlit") is None:
        pytest.fail("Install monitor test dependencies, or use -m 'not monitor'")
    ren_port, monitor_port = port(), port()
    while monitor_port == ren_port:
        monitor_port = port()
    argv = (
        sys.executable,
        "-u",
        "-m",
        "workbench_pilot.monitor_launcher",
        "--web",
        "--ren-port",
        str(ren_port),
        "--monitor-port",
        str(monitor_port),
        "--data-dir",
        str(tmp_path / "synthetic-data"),
    )
    env = safe_child_env({"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    launcher = OwnedProcessMonitor(argv, env=env, stop_timeout=15)
    # An unrelated owned test listener must remain available after monitor shutdown.
    with socket.socket() as unrelated:
        unrelated.bind(("127.0.0.1", 0))
        unrelated.listen()
        unrelated_port = unrelated.getsockname()[1]
        try:
            assert launcher.start().result(5)
            wait_for(lambda: "Ren output monitor:" in launcher.snapshot()["text"])
            with httpx.Client(trust_env=False, timeout=5) as client:
                assert client.get(f"http://127.0.0.1:{monitor_port}/").status_code == 200
                ren_url = f"http://127.0.0.1:{ren_port}"
                home = client.get(ren_url + "/")
                assert home.status_code == 200
                client.headers["X-Pilot-CSRF"] = re.search(
                    r'name="pilot-csrf" content="([0-9a-f]+)"', home.text
                )[1]
                response = client.post(
                    ren_url + "/api/pilot/runs",
                    json={"request_id": "monitor-stop-owned-browser", "fixture_id": "kickoff"},
                )
                assert response.status_code == 200
                run_id = response.json()["id"]
                wait_for(
                    lambda: client.get(ren_url + "/api/pilot/runs/" + run_id).json()["status"] == "pending"
                )
            descendants = owned_descendants(launcher.snapshot()["pid"])
            assert len(descendants) >= 3, "Expected launcher-owned Ren, Streamlit and browser descendants"
            assert launcher.stop().result(25)
            assert launcher.snapshot()["state"] == "exited"
            assert launcher.snapshot()["returncode"] == 0
            wait_for(lambda: closed(ren_port) and closed(monitor_port), timeout=5)
            assert not closed(unrelated_port)
            wait_for(lambda: all(not_executing(pid) for pid in descendants), timeout=5)
        finally:
            launcher.close()
