"""Tests for Wren Unified File Ingest REST API endpoints."""
import pytest
from fastapi.testclient import TestClient

from wren.core.controller import WrenController
from wren.files.ingest import UnifiedFileIngest
from wren.ui.orb_preview import create_app


@pytest.fixture
def temp_roots(tmp_path):
    local_dir = tmp_path / "Dev"
    local_dir.mkdir()
    (local_dir / "project_brief.md").write_text("This is the project brief content.", encoding="utf-8")
    (local_dir / ".secret_key").write_text("do not read me", encoding="utf-8")

    cloud_dir = tmp_path / "My Drive"
    cloud_dir.mkdir()
    (cloud_dir / "campaign_notes.txt").write_text("Cloud campaign notes.", encoding="utf-8")

    return local_dir, cloud_dir


@pytest.fixture
def test_client(temp_roots):
    local_dir, cloud_dir = temp_roots
    controller = WrenController(stt_provider="mock")
    controller.file_ingest = UnifiedFileIngest(
        local_roots=[str(local_dir)],
        cloud_roots=[str(cloud_dir)],
    )
    app = create_app(controller)
    return TestClient(app)


def test_api_files_roots(test_client, temp_roots):
    local_dir, cloud_dir = temp_roots
    response = test_client.get("/api/files/roots")
    assert response.status_code == 200
    data = response.json()
    assert "roots" in data
    assert str(local_dir) in data["roots"]["local"]
    assert str(cloud_dir) in data["roots"]["cloud"]


def test_api_files_search(test_client):
    response = test_client.get("/api/files/search?q=brief")
    assert response.status_code == 200
    results = response.json().get("results", [])
    assert len(results) == 1
    assert results[0]["name"] == "project_brief.md"
    assert results[0]["source"] == "local"


def test_api_files_read_allowed(test_client, temp_roots):
    local_dir, _ = temp_roots
    file_path = str(local_dir / "project_brief.md")
    response = test_client.post("/api/files/read", json={"file_path": file_path})
    assert response.status_code == 200
    data = response.json()
    assert data["path"] == file_path
    assert "This is the project brief content." in data["content"]


def test_api_files_read_forbidden(test_client, temp_roots):
    local_dir, _ = temp_roots
    file_path = str(local_dir / ".secret_key")
    response = test_client.post("/api/files/read", json={"file_path": file_path})
    assert response.status_code == 403


def test_api_files_attach_context(test_client, temp_roots):
    local_dir, _ = temp_roots
    file_path = str(local_dir / "project_brief.md")
    response = test_client.post("/api/files/attach", json={"file_path": file_path})
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert "project_brief.md" in data["attached_name"]
