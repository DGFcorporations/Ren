"""Offline policy and per-process data-directory lock. No provider configuration is accepted."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PilotSettings:
    data_dir: Path
    origin: str
    approval_ttl_seconds: float = 300
    allow_fault_injection: bool = True

    def __post_init__(self):
        from .browser import _parse_origin

        _parse_origin(self.origin)
        if not 0 < self.approval_ttl_seconds <= 300:
            raise ValueError("Approval TTL must be greater than zero and at most 300 seconds")
        object.__setattr__(self, "data_dir", Path(self.data_dir).expanduser().resolve())


class InstanceLock:
    """Prevent a second live process from invalidating the first one's pending approvals."""

    def __init__(self, path: Path):
        self.path = path
        self.file = None

    def acquire(self):
        if os.name != "posix":
            raise RuntimeError("G1 reference runtime is POSIX/Linux; other platforms are unverified")
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = self.path.open("a+")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            f.close()
            raise RuntimeError("Another pilot process owns this data directory") from exc
        self.file = f

    def close(self):
        if self.file:
            import fcntl

            fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()
            self.file = None
