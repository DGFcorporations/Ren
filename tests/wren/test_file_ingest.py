"""Tests for Wren Unified File Ingest (Local repos + Google Drive cloud)."""
import tempfile
from pathlib import Path

import pytest

from wren.files.ingest import FileIngestError, UnifiedFileIngest


@pytest.fixture
def temp_roots():
    with tempfile.TemporaryDirectory() as local_dir, tempfile.TemporaryDirectory() as cloud_dir:
        # Create sample local file
        local_p = Path(local_dir)
        (local_p / "notes.txt").write_text("Local dev notes about project architecture.")
        (local_p / "sub").mkdir()
        (local_p / "sub" / "code.py").write_text("def test(): pass")
        
        # Create sample cloud file
        cloud_p = Path(cloud_dir)
        (cloud_p / "Agent_Evidence").mkdir()
        (cloud_p / "Agent_Evidence" / "audit.md").write_text("# Cloud Google Drive Audit Report")
        (cloud_p / "client_list.csv").write_text("name,region\nClinic Alpha,Flagler")
        
        yield local_p, cloud_p


def test_file_ingest_detects_roots(temp_roots):
    local_p, cloud_p = temp_roots
    ingest = UnifiedFileIngest(local_roots=[str(local_p)], cloud_roots=[str(cloud_p)])
    
    roots = ingest.get_available_roots()
    assert str(local_p) in roots["local"]
    assert str(cloud_p) in roots["cloud"]


def test_search_across_local_and_cloud(temp_roots):
    local_p, cloud_p = temp_roots
    ingest = UnifiedFileIngest(local_roots=[str(local_p)], cloud_roots=[str(cloud_p)])
    
    # Search for notes
    res_local = ingest.search("notes")
    assert len(res_local) >= 1
    assert any("notes.txt" in r["path"] for r in res_local)
    assert res_local[0]["source"] == "local"

    # Search for audit in cloud
    res_cloud = ingest.search("audit")
    assert len(res_cloud) >= 1
    assert any("audit.md" in r["path"] for r in res_cloud)
    assert res_cloud[0]["source"] == "cloud"


def test_read_file_content(temp_roots):
    local_p, cloud_p = temp_roots
    ingest = UnifiedFileIngest(local_roots=[str(local_p)], cloud_roots=[str(cloud_p)])
    
    audit_file = str(cloud_p / "Agent_Evidence" / "audit.md")
    content = ingest.read_file(audit_file)
    assert "# Cloud Google Drive Audit Report" in content


def test_read_file_rejects_unauthorized_paths(temp_roots):
    local_p, cloud_p = temp_roots
    ingest = UnifiedFileIngest(local_roots=[str(local_p)], cloud_roots=[str(cloud_p)])
    
    with pytest.raises(FileIngestError, match="outside authorized roots"):
        ingest.read_file("/etc/passwd")
