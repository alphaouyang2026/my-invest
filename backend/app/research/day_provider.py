"""Filesystem lifecycle for direct-experiment Qlib day providers."""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.market_data import DataSnapshot
from app.research.day_provider_export import (
    EXPORTER_SCHEMA_VERSION,
    PYQLIB_VERSION,
    DayProviderManifest,
    export_snapshot_day_provider,
    load_day_provider_manifest,
)
from app.research.qlib_runtime import read_features


class DayProviderError(RuntimeError):
    """The requested provider cannot be safely built, opened, or deleted."""


@dataclass(frozen=True)
class DayProviderRef:
    path: Path
    manifest: DayProviderManifest


@dataclass(frozen=True)
class BuildDayProviderResult:
    provider: DayProviderRef
    created: bool


def provider_version_name() -> str:
    return f"schema-{EXPORTER_SCHEMA_VERSION}_pyqlib-{PYQLIB_VERSION}"


def provider_path(snapshot_id: uuid.UUID, provider_root: Path) -> Path:
    return Path(provider_root).resolve() / str(snapshot_id) / provider_version_name()


def open_day_provider(snapshot_id: uuid.UUID, provider_root: Path) -> DayProviderRef:
    """Validate current identity, native layout, and real Qlib readability."""
    path = provider_path(snapshot_id, provider_root)
    if not path.is_dir():
        raise DayProviderError(f"provider_not_found: {path}")
    return _validate_provider(path, snapshot_id)


def _validate_provider(path: Path, snapshot_id: uuid.UUID) -> DayProviderRef:
    try:
        manifest = load_day_provider_manifest(path / "manifest.json")
    except ValueError as exc:
        raise DayProviderError(str(exc)) from exc
    if manifest.snapshot_id != str(snapshot_id):
        raise DayProviderError(
            f"Provider snapshot mismatch: expected {snapshot_id}, got {manifest.snapshot_id}"
        )
    if manifest.exporter_schema_version != EXPORTER_SCHEMA_VERSION:
        raise DayProviderError(
            "Provider exporter schema mismatch: "
            f"expected {EXPORTER_SCHEMA_VERSION}, got {manifest.exporter_schema_version}"
        )
    if manifest.pyqlib_version != PYQLIB_VERSION:
        raise DayProviderError(
            f"Provider pyqlib mismatch: expected {PYQLIB_VERSION}, got {manifest.pyqlib_version}"
        )
    required = (path / "calendars" / "day.txt", path / "instruments" / "all.txt", path / "features")
    missing = [str(item) for item in required if not item.exists()]
    if missing:
        raise DayProviderError(f"Provider is missing required paths: {missing}")
    required_fields = {"close", "factor", "vwap"}
    available_fields = {field.lower() for field in manifest.fields}
    if not required_fields <= available_fields:
        raise DayProviderError(
            f"Provider lacks smoke fields {sorted(required_fields - available_fields)}"
        )
    instrument, active_start, active_end = _first_instrument(path / "instruments" / "all.txt")
    try:
        smoke = read_features(
            path,
            instruments=[instrument],
            fields=["$close", "$factor", "$vwap"],
            start=active_start,
            end=active_end,
        )
    except Exception as exc:
        raise DayProviderError(f"Qlib provider smoke read failed: {exc}") from exc
    if smoke.empty or "$close" not in smoke or not smoke["$close"].notna().any():
        raise DayProviderError("Qlib provider smoke read returned no finite close")
    return DayProviderRef(path=path, manifest=manifest)


def build_day_provider(
    session: Session,
    snapshot_id: uuid.UUID,
    provider_root: Path,
) -> BuildDayProviderResult:
    snapshot = session.get(DataSnapshot, snapshot_id)
    if snapshot is None:
        raise DayProviderError(f"DataSnapshot {snapshot_id} does not exist")
    if snapshot.source != "jquants":
        raise DayProviderError(
            f"DataSnapshot {snapshot_id} has unsupported source {snapshot.source!r}"
        )

    final = provider_path(snapshot_id, provider_root)
    if final.exists():
        return BuildDayProviderResult(open_day_provider(snapshot_id, provider_root), created=False)

    final.parent.mkdir(parents=True, exist_ok=True)
    temporary = final.parent / f".{final.name}.tmp-{uuid.uuid4()}"
    temporary.mkdir()
    try:
        export_snapshot_day_provider(session, snapshot, temporary)
        _validate_provider(temporary, snapshot_id)
        try:
            os.replace(temporary, final)
        except OSError:
            if not final.exists():
                raise
            shutil.rmtree(temporary, ignore_errors=True)
            return BuildDayProviderResult(
                open_day_provider(snapshot_id, provider_root), created=False
            )
        return BuildDayProviderResult(open_day_provider(snapshot_id, provider_root), created=True)
    except DayProviderError:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(temporary, ignore_errors=True)
        raise DayProviderError(f"Provider build failed: {exc}") from exc


def delete_day_provider(snapshot_id: uuid.UUID, provider_root: Path) -> bool:
    root = Path(provider_root).resolve()
    target = (root / str(snapshot_id)).resolve()
    if target.parent != root:
        raise DayProviderError(f"Provider delete target escapes root: {target}")
    if not target.exists():
        return False
    if not target.is_dir():
        raise DayProviderError(f"Provider delete target is not a directory: {target}")

    versions = [item for item in target.iterdir() if item.is_dir()]
    if not versions:
        raise DayProviderError(f"Provider contains no version directories: {target}")
    for version in versions:
        try:
            manifest = load_day_provider_manifest(version / "manifest.json")
        except ValueError as exc:
            raise DayProviderError(str(exc)) from exc
        if manifest.snapshot_id != str(snapshot_id):
            raise DayProviderError(
                f"Refusing delete: {version} belongs to snapshot {manifest.snapshot_id}"
            )
    shutil.rmtree(target)
    return True


def _first_instrument(path: Path) -> tuple[str, str, str]:
    try:
        first = next(line for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        instrument, start, end = first.split("\t")
    except (OSError, StopIteration, ValueError) as exc:
        raise DayProviderError(f"Invalid Qlib instruments file {path}: {exc}") from exc
    return instrument, start, end
