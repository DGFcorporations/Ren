"""Safe network defaults for the Wren desktop launcher."""
import sys
from unittest.mock import patch

import pytest

from wren.cli import main


def test_cli_binds_to_loopback_by_default(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["wren"])
    with patch("wren.cli.uvicorn.run") as run_server:
        main()

    assert run_server.call_args.kwargs["host"] == "127.0.0.1"


def test_cli_rejects_direct_nonloopback_host(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["wren", "--host", "0.0.0.0"])
    with patch("wren.cli.uvicorn.run") as run_server:
        with pytest.raises(SystemExit):
            main()

    run_server.assert_not_called()
