"""Pairing token is generated locally and never taken from a built-in password."""

import hashlib
import re
import stat

from fastapi.testclient import TestClient

from wren.core.controller import WrenController
from wren.ui.orb_preview import RETIRED_TOKEN_SHA256, create_app, get_network_ips, resolve_auth_token


def test_token_file_is_private_and_stable(tmp_path, monkeypatch):
    monkeypatch.delenv("WREN_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    token, replaced = resolve_auth_token(None, persist=True)
    path = tmp_path / "wren" / "auth-token"

    assert replaced is False
    assert path.read_text(encoding="utf-8").strip() == token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again, _replaced = resolve_auth_token(None, persist=True)
    assert again == token


def test_explicit_token_beats_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("WREN_AUTH_TOKEN", "env-token-value")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    token, replaced = resolve_auth_token("cli-token-value", persist=False)
    assert token == "cli-token-value"
    assert replaced is False


def test_retired_digest_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("WREN_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    real_sha256 = hashlib.sha256

    def fake_sha256(data):
        if data == b"stand-in":
            class Digest:
                def hexdigest(self):
                    return RETIRED_TOKEN_SHA256
            return Digest()
        return real_sha256(data)

    monkeypatch.setattr("wren.ui.orb_preview.hashlib.sha256", fake_sha256)
    token, replaced = resolve_auth_token("stand-in", persist=True)
    assert replaced is True
    assert token != "stand-in"
    assert (tmp_path / "wren" / "auth-token").read_text(encoding="utf-8").strip() == token


def test_symlink_token_file_is_not_followed(tmp_path, monkeypatch):
    monkeypatch.delenv("WREN_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    outside = tmp_path / "outside-secret"
    outside.write_text("do-not-read\n", encoding="utf-8")
    store = tmp_path / "wren"
    store.mkdir()
    (store / "auth-token").symlink_to(outside)

    token, _replaced = resolve_auth_token(None, persist=True)
    assert token != "do-not-read"
    assert outside.read_text(encoding="utf-8") == "do-not-read\n"
    assert not (store / "auth-token").is_symlink()


def test_no_baked_in_address_when_detection_fails(monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr("wren.ui.orb_preview.socket.socket", boom)
    monkeypatch.setattr("wren.ui.orb_preview.subprocess.run", boom)
    assert get_network_ips() == {"lan_ip": "", "tailscale_ip": ""}


def test_index_does_not_publish_a_personal_address(monkeypatch, tmp_path):
    monkeypatch.delenv("WREN_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    controller = WrenController(stt_provider="mock", model="auto")
    app = create_app(controller, auth_token="page-token")
    body = TestClient(app).get("/").text
    connect = body.split('id="connect-body"', 1)[1]
    assert "not detected" in connect
    assert re.search(r"\d+\.\d+\.\d+\.\d+", connect) is None
