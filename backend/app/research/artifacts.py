from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from app.core.logging import get_logger

logger = get_logger(__name__)

ARTIFACT_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class StagedArtifact:
    """What was written to staging, before anything is published."""

    logical_checksum: str
    size_bytes: int
    manifest: dict


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ArtifactStaging:
    """Writes an artifact directory and checksums it. Publishes nothing.

    Split out of the old `ResearchArtifactWriter`, which wrote *and* renamed
    into place in one call. The rename is now the publisher's business
    (`research/publication.py`) because it has to be ordered against a database
    transaction, and that ordering is the whole point of the protocol. Keeping
    the byte-writing pure leaves it testable without a session.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def write(
        self,
        *,
        run_id: uuid.UUID,
        tables: dict[str, pd.DataFrame],
        summary: dict,
        warnings: list[dict],
        runtime_identity: dict,
        extra_files: dict[str, bytes] | None = None,
    ) -> StagedArtifact:
        self.directory.parent.mkdir(parents=True, exist_ok=True)
        self.directory.mkdir(parents=True, exist_ok=False)
        try:
            file_entries: dict[str, dict] = {}
            for name, frame in sorted(tables.items()):
                if not name.replace("_", "").isalnum():
                    raise ValueError(f"Invalid artifact table name {name!r}")
                filename = f"{name}.parquet"
                path = self.directory / filename
                frame.to_parquet(path, index=False)
                file_entries[filename] = {
                    "rows": len(frame),
                    "bytes": path.stat().st_size,
                    "sha256": _checksum(path),
                }
                logger.debug(
                    "research_artifact.table_written",
                    table=name,
                    rows=len(frame),
                    bytes=file_entries[filename]["bytes"],
                )

            for filename, payload in sorted((extra_files or {}).items()):
                path = self.directory / filename
                if path.parent != self.directory:
                    raise ValueError(f"Artifact file {filename!r} escapes the artifact directory")
                path.write_bytes(payload)
                file_entries[filename] = {
                    "bytes": path.stat().st_size,
                    "sha256": _checksum(path),
                }

            summary_path = self.directory / "summary.json"
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            file_entries["summary.json"] = {
                "bytes": summary_path.stat().st_size,
                "sha256": _checksum(summary_path),
            }
            logical_checksum = hashlib.sha256(
                json.dumps(file_entries, separators=(",", ":"), sort_keys=True).encode("utf-8")
            ).hexdigest()
            manifest = {
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "research_run_id": str(run_id),
                "logical_checksum": logical_checksum,
                "runtime_identity": runtime_identity,
                "warnings": warnings,
                "files": file_entries,
            }
            (self.directory / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except Exception:
            shutil.rmtree(self.directory, ignore_errors=True)
            raise

        size_bytes = sum(path.stat().st_size for path in self.directory.iterdir() if path.is_file())
        logger.info(
            "research_artifact.staged",
            directory=self.directory.name,
            tables=sorted(tables),
            size_bytes=size_bytes,
            logical_checksum=logical_checksum,
        )
        return StagedArtifact(logical_checksum, size_bytes, manifest)


class ResearchArtifactReader:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def _directory(self, relative_path: str) -> Path:
        directory = (self.root / relative_path).resolve()
        if self.root not in directory.parents:
            raise ValueError("Artifact path escapes configured root")
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("schema_version") != ARTIFACT_SCHEMA_VERSION:
            raise ValueError(f"Unsupported research artifact schema {manifest.get('schema_version')!r}")
        return directory

    def summary(self, relative_path: str) -> dict:
        return json.loads((self._directory(relative_path) / "summary.json").read_text(encoding="utf-8"))

    def table(self, relative_path: str, name: str) -> pd.DataFrame:
        directory = self._directory(relative_path)
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        filename = f"{name}.parquet"
        if filename not in manifest["files"]:
            raise KeyError(f"Artifact table {name!r} does not exist")
        return pd.read_parquet(directory / filename)
