"""Wren Unified File Ingest: Local workspace repos & Google Drive cloud files."""
import os
from pathlib import Path


class FileIngestError(ValueError):
    """Raised when file access is disallowed or invalid."""
    pass


FORBIDDEN_PATTERNS = {
    ".secrets",
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    ".env",
    "_agent-scratch",
    "logs",
    ".cache",
    "target",
    "dist",
    "build",
    "g",
}

DEFAULT_LINUX_LOCAL_ROOT = "/home/chandler/Dev"
DEFAULT_LINUX_CLOUD_ROOT = "/home/chandler/Dev/g/My Drive"

DEFAULT_WIN_LOCAL_ROOT = r"C:\Dev"
DEFAULT_WIN_CLOUD_ROOT = r"G:\My Drive"


class UnifiedFileIngest:
    """Manages file discovery and safe reading across local repos and Google Drive."""

    def __init__(
        self,
        local_roots: list[str] | None = None,
        cloud_roots: list[str] | None = None,
        max_file_size_bytes: int = 500 * 1024,
    ):
        is_win = os.name == "nt"
        if local_roots is None:
            default_local = DEFAULT_WIN_LOCAL_ROOT if is_win else DEFAULT_LINUX_LOCAL_ROOT
            self.local_roots = [default_local] if Path(default_local).exists() else []
        else:
            self.local_roots = [str(Path(r).resolve()) for r in local_roots]

        if cloud_roots is None:
            default_cloud = DEFAULT_WIN_CLOUD_ROOT if is_win else DEFAULT_LINUX_CLOUD_ROOT
            self.cloud_roots = [default_cloud] if Path(default_cloud).exists() else []
        else:
            self.cloud_roots = [str(Path(r).resolve()) for r in cloud_roots]

        self.max_file_size_bytes = max_file_size_bytes

    def get_available_roots(self) -> dict[str, list[str]]:
        return {
            "local": [r for r in self.local_roots if Path(r).exists()],
            "cloud": [r for r in self.cloud_roots if Path(r).exists()],
        }

    def _is_safe_path(self, target_path: Path) -> bool:
        resolved = target_path.resolve()

        all_authorized = [Path(r).resolve() for r in (self.local_roots + self.cloud_roots)]
        matched_root = None
        for root in all_authorized:
            if resolved == root or root in resolved.parents:
                matched_root = root
                break

        if not matched_root:
            return False

        rel_parts = resolved.relative_to(matched_root).parts
        for part in rel_parts:
            part_lower = part.lower()
            if (
                part in FORBIDDEN_PATTERNS
                or part.startswith(".")
                or part_lower.endswith(".env")
                or "secret" in part_lower
            ):
                return False

        return True

    def search(self, query: str, max_results: int = 15) -> list[dict[str, str]]:
        """Search for files matching the query in filename across local and cloud roots."""
        results = []
        q_lower = query.lower()

        for source_name, roots in [("local", self.local_roots), ("cloud", self.cloud_roots)]:
            for root in roots:
                root_path = Path(root).resolve()
                if not root_path.exists():
                    continue
                max_depth = 2 if source_name == "cloud" else 3
                for dirpath, dirnames, filenames in os.walk(root):
                    try:
                        curr_depth = len(Path(dirpath).resolve().relative_to(root_path).parts)
                        if curr_depth >= max_depth:
                            dirnames.clear()
                    except ValueError:
                        dirnames.clear()
                        continue

                    if source_name == "cloud":
                        # Bound cloud breadth on FUSE mounts to avoid network lag
                        dirnames[:] = [
                            d for d in dirnames
                            if not d.startswith(".") and d not in FORBIDDEN_PATTERNS
                        ][:15]
                    else:
                        # Prune hidden, forbidden, and cloud-mount directories during local walk
                        dirnames[:] = [
                            d
                            for d in dirnames
                            if not d.startswith(".")
                            and d not in FORBIDDEN_PATTERNS
                            and "secret" not in d.lower()
                            and not (source_name == "local" and d == "g")
                        ]
                    for fname in filenames:
                        if (
                            fname.startswith(".")
                            or fname.endswith(".env")
                            or "secret" in fname.lower()
                        ):
                            continue
                        if q_lower in fname.lower():
                            full_path = Path(dirpath) / fname
                            results.append({
                                "name": fname,
                                "path": str(full_path.resolve()),
                                "source": source_name,
                            })
                            if len(results) >= max_results:
                                return results

        return results

    def read_file(self, file_path: str) -> str:
        """Safely read text or markdown file content within size limits."""
        path = Path(file_path).resolve()
        if not self._is_safe_path(path):
            raise FileIngestError(
                f"Access denied: path '{file_path}' is outside authorized roots or forbidden"
            )

        if not path.is_file():
            raise FileIngestError(f"File not found or not a regular file: '{file_path}'")

        size = path.stat().st_size
        if size > self.max_file_size_bytes:
            raise FileIngestError(
                f"File size ({size} bytes) exceeds maximum allowable limit ({self.max_file_size_bytes} bytes)"
            )

        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            raise FileIngestError(f"Failed to read file '{file_path}': {e}") from e
