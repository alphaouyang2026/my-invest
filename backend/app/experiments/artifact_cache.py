"""Content-addressed artifacts on the filesystem, and the lock that guards them.

Both experiment entry points write derived data that is expensive to rebuild and
useless if half-written, so both need the same three guarantees: a directory is
either complete or absent, completeness is proven by checksum rather than by the
directory existing, and an identity mismatch is an error rather than a cache miss.

These lived in `search_plan` while only the search had artifacts. The direct run
now caches feature shards too, and it must not import the search to do so.
"""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                     allow_nan=False).encode()).hexdigest()


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, default=str,
                                    allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)


def write_parquet(path: Path, frame) -> None:
    """Atomic like `write_json`: a reader never sees a half-written table."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    frame.to_parquet(temporary)
    os.replace(temporary, path)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@contextmanager
def exclusive_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".running.lock"
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lock.unlink()


def seal(root: Path, identity: str) -> None:
    files = {str(p.relative_to(root)): file_digest(p) for p in sorted(root.rglob("*"))
             if p.is_file() and p.name not in {"complete.json", ".running.lock"} and not p.name.endswith(".tmp")}
    write_json(root / "complete.json", {"identity": identity, "files": files})


def verified(root: Path, identity: str) -> bool:
    marker = root / "complete.json"
    if not marker.exists():
        return False
    value = read_json(marker)
    if value["identity"] != identity:
        raise ValueError(f"Artifact identity mismatch: {root}")
    for name, checksum in value["files"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file() or file_digest(path) != checksum:
            raise ValueError(f"Artifact checksum mismatch: {name}")
    return True
