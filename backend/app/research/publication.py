"""Getting a directory and a set of database rows to agree, across a crash.

`os.replace` and a Postgres transaction cannot commit together, so one of them
happens first and a worker can die in between. Ticket 06 published the directory
first and inserted the row afterwards, which on a crash leaves a *published*
directory that no row mentions, while recovery marks the run failed — a failed
run with artifacts on disk, which the design forbids.

The order here is inverted, and the window is closed with a status rather than
with hope:

    1. write the staging directory and checksum it
    2. one transaction: every database row, plus a `prepared` publication
    3. os.replace(staging, final)
    4. one transaction: publication -> committed

Every crash point is recoverable because after step 2 the database already knows
everything. Recovery never has to reconstruct a row from a file:

    prepared + final present  -> the rename happened; flip to committed
    prepared + staging present -> the rename did not; redo it, then commit
    prepared + neither        -> nothing to publish; mark failed and fail the run

The remaining subtlety is that step 2 marks the run succeeded while the
directory is not yet in place. That is why **a reader must require
`status == committed`**, not merely the presence of a `ResearchArtifact` row
(`readable_artifact`). "The row exists" and "the bytes are in place" are
different facts, and only the publication row distinguishes them.
"""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.research import (
    ArtifactPublicationStatus,
    ResearchArtifact,
    ResearchArtifactPublication,
    ResearchRun,
    ResearchRunStatus,
)
from app.research.artifacts import ARTIFACT_SCHEMA_VERSION, ArtifactStaging

logger = get_logger(__name__)

#: Staging directories are named so a sweep can recognise them. A crash before
#: step 2 leaves one with no row at all, and only the prefix identifies it as
#: ours rather than something a user dropped in the directory.
STAGING_PREFIX = ".staging-"


class PublicationError(RuntimeError):
    """The artifact could not be published or recovered."""


@dataclass(frozen=True)
class PreparedPublication:
    publication_id: uuid.UUID
    research_run_id: uuid.UUID
    relative_path: str
    staging_path: str
    logical_checksum: str
    size_bytes: int
    manifest: dict


class ResearchArtifactPublisher:
    """Shared by factor and model runs; the protocol does not care which."""

    def __init__(self, session: Session, root: Path) -> None:
        self.session = session
        # Resolved on the way in, because the containment guard below compares
        # against `final.resolve()`. The configured default is *relative*
        # (`var/research-artifacts`), so leaving it unresolved makes that guard
        # compare a relative root against an absolute parent list and reject
        # every path, including the legitimate one. `ResearchArtifactReader`
        # resolves for the same reason.
        self.root = Path(root).resolve()

    # -- step 1 + 2 --------------------------------------------------------

    def prepare(
        self,
        run: ResearchRun,
        *,
        tables: dict[str, pd.DataFrame],
        summary: dict,
        warnings: list[dict],
        runtime_identity: dict,
        extra_files: dict[str, bytes] | None = None,
    ) -> PreparedPublication:
        """Stage the bytes and record the intent to publish them.

        Returns with the publication row **committed**. The caller adds its own
        rows to the same session before calling `commit`, so that everything the
        database needs to know lands in one transaction.
        """
        final = self.root / str(run.id)
        if final.exists():
            raise PublicationError(f"Research artifact already published for {run.id}")

        staging = self.root / f"{STAGING_PREFIX}{run.id}"
        if staging.exists():
            # Left by an earlier attempt whose publication row never committed;
            # its bytes are not referenced by anything, so it is safe to drop.
            shutil.rmtree(staging, ignore_errors=True)

        written = ArtifactStaging(staging).write(
            run_id=run.id,
            tables=tables,
            summary=summary,
            warnings=warnings,
            runtime_identity=runtime_identity,
            extra_files=extra_files or {},
        )

        publication = ResearchArtifactPublication(
            research_run_id=run.id,
            status=ArtifactPublicationStatus.PREPARED,
            relative_path=str(run.id),
            staging_path=staging.name,
            logical_checksum=written.logical_checksum,
        )
        self.session.add(publication)
        self.session.flush()
        logger.info(
            "research_artifact.prepared",
            research_run_id=str(run.id),
            logical_checksum=written.logical_checksum,
            size_bytes=written.size_bytes,
        )
        return PreparedPublication(
            publication_id=publication.id,
            research_run_id=run.id,
            relative_path=str(run.id),
            staging_path=staging.name,
            logical_checksum=written.logical_checksum,
            size_bytes=written.size_bytes,
            manifest=written.manifest,
        )

    def artifact_row(self, prepared: PreparedPublication) -> ResearchArtifact:
        """The `ResearchArtifact` for a prepared publication.

        Handed back rather than added here so the caller inserts it alongside
        its own rows — a model run also writes `TrainedModel` and
        `PredictionRun`, and all of them have to share one transaction.
        """
        return ResearchArtifact(
            research_run_id=prepared.research_run_id,
            schema_version=prepared.manifest["schema_version"],
            relative_path=prepared.relative_path,
            logical_checksum=prepared.logical_checksum,
            manifest=prepared.manifest,
            size_bytes=prepared.size_bytes,
        )

    # -- step 3 + 4 --------------------------------------------------------

    def commit(self, prepared: PreparedPublication) -> None:
        """Move the bytes into place, then mark the publication committed.

        The caller must have committed its rows first: this method assumes the
        database is already complete and only the filesystem is behind.
        """
        publication = self.session.get(ResearchArtifactPublication, prepared.publication_id)
        if publication is None:
            raise PublicationError(f"Publication {prepared.publication_id} disappeared")

        self._move_into_place(publication)
        publication.status = ArtifactPublicationStatus.COMMITTED
        publication.committed_at = datetime.now(timezone.utc)
        publication.staging_path = None
        self.session.commit()
        logger.info(
            "research_artifact.committed",
            research_run_id=str(prepared.research_run_id),
            relative_path=prepared.relative_path,
        )

    def abandon(self, prepared: PreparedPublication, reason: str) -> None:
        """Give up on a prepared publication and remove its bytes."""
        publication = self.session.get(ResearchArtifactPublication, prepared.publication_id)
        if publication is None:
            return
        shutil.rmtree(self.root / prepared.staging_path, ignore_errors=True)
        publication.status = ArtifactPublicationStatus.FAILED
        publication.error_summary = reason
        publication.staging_path = None
        self.session.commit()
        logger.info(
            "research_artifact.abandoned", research_run_id=str(prepared.research_run_id), reason=reason
        )

    def _move_into_place(self, publication: ResearchArtifactPublication) -> None:
        final = self.root / publication.relative_path
        if final.exists():
            # The rename already happened before the crash. Idempotent by
            # design: recovery replays this method and must not fail here.
            return
        if not publication.staging_path:
            raise PublicationError(
                f"Publication {publication.id} has neither a published directory nor staging"
            )
        staging = self.root / publication.staging_path
        if not staging.exists():
            raise PublicationError(f"Staging directory {staging} is gone")
        if self.root not in final.resolve().parents:
            raise PublicationError("Artifact path escapes the configured root")
        os.replace(staging, final)


def recover_publications(session: Session, root: Path) -> list[uuid.UUID]:
    """Resolve every `prepared` publication left behind by a dead worker.

    Called at worker start, before any task is picked up, so no run observes a
    half-published artifact. Returns the runs that were failed.
    """
    # Same reason as the publisher's constructor: the configured default is a
    # relative path, and everything downstream compares against resolved ones.
    root = Path(root).resolve()
    failed: list[uuid.UUID] = []
    pending = session.scalars(
        select(ResearchArtifactPublication).where(
            ResearchArtifactPublication.status == ArtifactPublicationStatus.PREPARED
        )
    ).all()

    for publication in pending:
        publisher = ResearchArtifactPublisher(session, root)
        try:
            publisher._move_into_place(publication)
        except PublicationError as exc:
            # Nothing on disk to publish. The database rows were committed in
            # step 2, so they have to go with it — otherwise a succeeded run
            # would point at a directory that never existed.
            publication.status = ArtifactPublicationStatus.FAILED
            publication.error_summary = str(exc)
            publication.staging_path = None
            _fail_run(session, publication.research_run_id, str(exc))
            failed.append(publication.research_run_id)
            session.commit()
            logger.warning(
                "research_artifact.recovery_failed",
                research_run_id=str(publication.research_run_id),
                reason=str(exc),
            )
            continue

        publication.status = ArtifactPublicationStatus.COMMITTED
        publication.committed_at = datetime.now(timezone.utc)
        publication.staging_path = None
        session.commit()
        logger.info(
            "research_artifact.recovery_committed",
            research_run_id=str(publication.research_run_id),
        )

    _sweep_orphan_staging(session, root)
    return failed


def _fail_run(session: Session, run_id: uuid.UUID, reason: str) -> None:
    run = session.get(ResearchRun, run_id)
    if run is None:
        return
    session.query(ResearchArtifact).filter(ResearchArtifact.research_run_id == run_id).delete()
    run.status = ResearchRunStatus.FAILED
    run.error_code = "artifact_publication_lost"
    run.error_summary = reason
    run.finished_at = datetime.now(timezone.utc)


def _sweep_orphan_staging(session: Session, root: Path) -> None:
    """Remove staging directories whose publication row never committed.

    These come from a crash between writing the bytes and committing step 2.
    Nothing references them, and left alone they accumulate silently until the
    disk budget trips.
    """
    if not root.exists():
        return
    known = {
        row
        for row in session.scalars(
            select(ResearchArtifactPublication.staging_path).where(
                ResearchArtifactPublication.staging_path.is_not(None)
            )
        ).all()
    }
    for entry in root.iterdir():
        if entry.is_dir() and entry.name.startswith(STAGING_PREFIX) and entry.name not in known:
            shutil.rmtree(entry, ignore_errors=True)
            logger.info("research_artifact.orphan_staging_removed", path=entry.name)


def readable_artifact(session: Session, run_id: uuid.UUID) -> ResearchArtifact | None:
    """The artifact of a run, but only once its bytes are actually in place.

    A `ResearchArtifact` row exists from step 2 onward, while the directory
    arrives at step 3. Readers that go by the row alone will occasionally open a
    path that is not there yet.
    """
    publication = session.scalar(
        select(ResearchArtifactPublication).where(
            ResearchArtifactPublication.research_run_id == run_id
        )
    )
    if publication is None or publication.status != ArtifactPublicationStatus.COMMITTED:
        return None
    return session.scalar(
        select(ResearchArtifact).where(ResearchArtifact.research_run_id == run_id)
    )
