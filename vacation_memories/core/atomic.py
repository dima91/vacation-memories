"""Atomic file writes: a crash or a power cut never leaves a truncated file behind."""

import contextlib
import json
import os
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to a temporary file in the same folder, then `os.replace` it over `path`."""
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    _fsync_folder(path.parent)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, data: object) -> None:
    """Serialize as ASCII-only JSON, so file names that are not valid UTF-8 (kept as surrogate
    escapes by `os.fsdecode`) survive a round trip."""
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=True) + "\n")


def _fsync_folder(folder: Path) -> None:
    try:
        descriptor = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
