"""Safety and status contracts for the Android build helper."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_android.sh"
ANDROID_DIR = REPO_ROOT / "android"


def _project_copy(tmp_path: Path) -> tuple[Path, Path]:
    """Copy only the build helper into a disposable project tree."""
    project = tmp_path / "project"
    script = project / "scripts" / "build_android.sh"
    script.parent.mkdir(parents=True)
    (project / "android" / "app").mkdir(parents=True)
    shutil.copyfile(BUILD_SCRIPT, script)
    return project, script


def _fake_path(tmp_path: Path, *, include_gradle: bool) -> Path:
    """Create a small PATH with only the shell utilities required by the helper."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("dirname", "awk"):
        actual = shutil.which(name)
        if actual:
            (bin_dir / name).symlink_to(actual)

    if include_gradle:
        gradle = bin_dir / "gradle"
        gradle.write_text(
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            'if [[ " $* " != *" assembleDebug "* ]]; then exit 9; fi\n'
            "/bin/mkdir -p app/build/outputs/apk/debug\n"
            "/usr/bin/touch app/build/outputs/apk/debug/app-debug.apk\n",
            encoding="utf-8",
        )
        gradle.chmod(0o755)

        adb = bin_dir / "adb"
        adb.write_text(
            "#!/bin/bash\n"
            "set -euo pipefail\n"
            'printf \'%s\\n\' "$*" >> "$ADB_CALL_LOG"\n'
            "if [[ \"${1-}\" == devices ]]; then printf 'List of devices attached\\nTEST123 device\\n'; fi\n",
            encoding="utf-8",
        )
        adb.chmod(0o755)
    return bin_dir


def _run(
    script: Path, project: Path, bin_dir: Path, tmp_path: Path, *args: str
) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PATH": str(bin_dir),
        "ANDROID_HOME": str(tmp_path / "android-sdk-stub"),
        "ANDROID_SDK_ROOT": "",
        "ADB_CALL_LOG": str(tmp_path / "adb-calls.txt"),
    }
    return subprocess.run(
        ["/bin/bash", str(script), *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_missing_gradle_fails_instead_of_claiming_success(tmp_path: Path) -> None:
    if (ANDROID_DIR / "gradlew").exists():
        pytest.skip("A checked-in wrapper is available; this test covers the no-Gradle path")

    project, script = _project_copy(tmp_path)
    bin_dir = _fake_path(tmp_path, include_gradle=False)
    result = _run(script, project, bin_dir, tmp_path)

    assert result.returncode == 127
    assert "no executable Gradle wrapper or system Gradle found" in result.stderr
    assert "Build successful" not in result.stdout


def test_android_build_does_not_install_without_explicit_flag(tmp_path: Path) -> None:
    if (ANDROID_DIR / "gradlew").exists():
        pytest.skip("A checked-in wrapper takes precedence over the controlled Gradle stub")

    project, script = _project_copy(tmp_path)
    bin_dir = _fake_path(tmp_path, include_gradle=True)
    result = _run(script, project, bin_dir, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Build successful" in result.stdout
    assert not (tmp_path / "adb-calls.txt").exists()
    assert (project / "android/app/build/outputs/apk/debug/app-debug.apk").is_file()


def test_android_install_requires_explicit_flag(tmp_path: Path) -> None:
    if (ANDROID_DIR / "gradlew").exists():
        pytest.skip("A checked-in wrapper takes precedence over the controlled Gradle stub")

    project, script = _project_copy(tmp_path)
    bin_dir = _fake_path(tmp_path, include_gradle=True)
    result = _run(script, project, bin_dir, tmp_path, "--install")

    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "adb-calls.txt").read_text(encoding="utf-8").splitlines()
    assert calls[0] == "devices"
    assert calls[1].startswith("install -r ")
