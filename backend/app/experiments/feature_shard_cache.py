"""Features on disk, split so that no step holds the whole panel.

A run needs one bounded segment at a time; Qlib hands back everything at once and
`DataHandlerLP` then keeps two processed copies of it. On a 360-column universe
that is the difference between a run and an out-of-memory kill, so features are
computed in security-sized batches, written as checksummed shards, and streamed
back into one preallocated matrix per segment.

Splitting by security rather than by date is forced: a forward label is computed
with `shift` along each security's own series, and cutting that series by date
would truncate the label near every boundary.

The cache belongs to the run rather than to any one caller. A search sweeping
parameters over one dataset computes it once; the command line reuses it between
invocations over the same span.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from app.experiments.artifact_cache import (
    digest,
    exclusive_lock,
    read_json,
    seal,
    verified,
    write_json,
    write_parquet,
)
from app.research.qlib_runtime import read_features

#: Bumped when a cached shard's meaning changes, so old caches are rebuilt
#: rather than silently reused under a new interpretation.
FEATURE_CACHE_SCHEMA_VERSION = 1
DEFAULT_FEATURE_BATCH_SIZE = 64
DEFAULT_QLIB_KERNELS = 2
DEFAULT_SCAN_BATCH_ROWS = 4096


class FeatureCacheError(RuntimeError):
    """A cache that cannot be trusted to answer the question being asked of it."""


@dataclass(frozen=True)
class DateSpan:
    start: date
    end: date


@dataclass(frozen=True)
class FeatureCacheSpec:
    """What a cache is built from: its contents, plus how the work is divided.

    Separate from the run's own configuration so that adding a knob there cannot
    quietly widen or narrow what identifies a cache. The first two fields decide
    the contents and so decide the identity; the three batch sizes decide only
    how the computation is split, and `cache_identity` deliberately ignores them.
    """

    span: DateSpan
    label_horizon: int
    feature_batch_size: int = DEFAULT_FEATURE_BATCH_SIZE
    qlib_kernels: int = DEFAULT_QLIB_KERNELS
    scan_batch_rows: int = DEFAULT_SCAN_BATCH_ROWS


@dataclass(frozen=True)
class MatrixSegment:
    """One bounded segment; feature storage is allocated exactly once."""

    features: np.ndarray
    labels: np.ndarray
    index: pd.MultiIndex
    feature_names: tuple[str, ...]

    def __len__(self) -> int:
        return len(self.index)

    @property
    def dates(self):
        return self.index.get_level_values("datetime")


@dataclass(frozen=True)
class ShardedDataset:
    """A verified feature cache, plus the bounded reader that trains from it.

    The panel is never held whole. Features stay split across per-security shards
    on disk and are streamed into one preallocated matrix per segment, so peak
    memory is the segment being trained on rather than the whole experiment. The
    split is by security rather than by date because a forward label is computed
    with `shift` along each security's own series, and cutting that series by date
    would silently truncate the label near every boundary.
    """

    root: Path
    feature_names: tuple[str, ...]
    label_horizon: int
    scan_batch_rows: int
    mature_dates: frozenset[date]
    covered_dates: frozenset[date]

    @property
    def covered(self) -> DateSpan:
        return DateSpan(min(self.covered_dates), max(self.covered_dates))

    def load_segment(self, bounds, *, learning: bool) -> MatrixSegment:
        import pyarrow.dataset as ds

        start, end = pd.Timestamp(bounds.start), pd.Timestamp(bounds.end)
        column = f"learn_h{self.label_horizon}" if learning else f"raw_h{self.label_horizon}"
        labels = pd.read_parquet(
            self.root / "labels.parquet",
            columns=["datetime", "instrument", column],
            filters=[("datetime", ">=", start), ("datetime", "<=", end)],
        )
        labels["datetime"] = pd.to_datetime(labels["datetime"])
        labels["instrument"] = labels["instrument"].astype(str)
        if labels.duplicated(["datetime", "instrument"]).any():
            raise FeatureCacheError("Duplicate label row in the feature cache")
        all_index = pd.MultiIndex.from_frame(labels[["datetime", "instrument"]])
        if learning:
            # DropnaLabel. A row whose label is unknown teaches nothing, and keeping
            # it would let CSRankNorm rank against information that is not there.
            labels = labels.loc[labels[column].notna()].copy()
        labels = labels.sort_values(["datetime", "instrument"], kind="stable")
        target = pd.MultiIndex.from_frame(labels[["datetime", "instrument"]])
        values = np.empty((len(target), len(self.feature_names)), dtype=np.float32)
        written = np.zeros(len(target), dtype=bool)
        paths = sorted(self.root.glob("batch-*/features.parquet"))
        if not paths:
            raise FeatureCacheError("Feature cache holds no shards")
        scanner = ds.dataset([str(path) for path in paths], format="parquet").scanner(
            columns=["datetime", "instrument", *self.feature_names],
            filter=(ds.field("datetime") >= start.to_datetime64())
            & (ds.field("datetime") <= end.to_datetime64()),
            batch_size=self.scan_batch_rows,
            batch_readahead=1,
            fragment_readahead=1,
            use_threads=False,
        )
        for batch in scanner.to_batches():
            frame = batch.to_pandas()
            keys = pd.MultiIndex.from_arrays(
                [pd.to_datetime(frame["datetime"]), frame["instrument"].astype(str)],
                names=target.names,
            )
            positions = target.get_indexer(keys)
            unknown = positions < 0
            if unknown.any() and (all_index.get_indexer(keys[unknown]) < 0).any():
                raise FeatureCacheError("Feature cache holds a row the labels do not")
            selected = positions >= 0  # The rest were dropped along with their labels.
            chosen = positions[selected]
            if len(np.unique(chosen)) != len(chosen) or written[chosen].any():
                raise FeatureCacheError("Duplicate feature row in the feature cache")
            values[chosen] = frame.loc[selected, list(self.feature_names)].to_numpy(
                dtype=np.float32, copy=False
            )
            written[chosen] = True
        if not written.all():
            raise FeatureCacheError(
                f"Feature cache is missing {int((~written).sum())} rows "
                f"for {bounds.start}..{bounds.end}"
            )
        return MatrixSegment(values, labels[column].to_numpy(), target, self.feature_names)

    def raw_labels(self) -> pd.DataFrame:
        """Unnormalised labels for evaluation, with why each missing one is missing."""
        column = f"raw_h{self.label_horizon}"
        labels = pd.read_parquet(
            self.root / "labels.parquet", columns=["datetime", "instrument", column]
        ).rename(
            columns={"datetime": "observation_date", "instrument": "instrument_id", column: "label"}
        )
        labels["observation_date"] = pd.to_datetime(labels["observation_date"]).dt.date
        labels["instrument_id"] = labels["instrument_id"].astype(str)
        labels["label_reason"] = np.where(
            labels["label"].notna(),
            None,
            np.where(
                labels["observation_date"].isin(self.mature_dates),
                "label_unavailable",
                "label_not_mature",
            ),
        )
        return labels


def restrict_to_universe(frame: pd.DataFrame, universe: dict[date, set[str]]) -> pd.DataFrame:
    dates = frame.index.get_level_values("datetime")
    instruments = frame.index.get_level_values("instrument")
    keep = [
        str(instrument) in universe.get(stamp.date(), ())
        for stamp, instrument in zip(dates, instruments)
    ]
    return frame.loc[keep]


def mature_label_dates(
    calendar,
    snapshot_coverage_end: date,
    label_horizon: int,
) -> set[date]:
    """Dates whose complete forward label lies inside snapshot bar coverage."""

    days = tuple(calendar)
    covered_sessions = bisect_right(days, snapshot_coverage_end)
    mature_count = max(0, covered_sessions - (label_horizon + 1))
    return set(days[:mature_count])


def cache_identity(provider, feature_set, spec, snapshot, policy_fingerprint: str) -> str:
    """What the cached features are a function of, and nothing else.

    Deliberately not the code digest an experiment search uses for its own
    identity: every edit anywhere would then discard a cache whose contents did
    not change, and this entry point would lose its cache on every commit. Nor
    the batch sizes, which change how the work is divided and not one cached
    value; a half-built cache is guarded by its shard plan instead.

    The span is enough only because the caller builds its pool for the whole
    span. Filtering the cache by a pool derived from a subset -- the days some
    fold layout happens to touch -- would put that layout into the contents
    without putting it here, and a later run would silently read short.
    """
    return digest(
        {
            "schema": FEATURE_CACHE_SCHEMA_VERSION,
            "snapshot": [str(snapshot.id), snapshot.version, snapshot.bar_publish_sequence],
            "provider": provider.manifest.logical_checksum,
            "feature_set": [feature_set.name, feature_set.definition_checksum],
            "label_horizon": spec.label_horizon,
            "policy": policy_fingerprint,
            "span": [spec.span.start, spec.span.end],
        }
    )


def assemble_sharded_dataset(
    provider,
    feature_set,
    spec: FeatureCacheSpec,
    universe: dict[date, set[str]],
    *,
    snapshot,
    policy_fingerprint: str,
    cache_root: Path,
    required_days,
) -> ShardedDataset:
    """Compute features once into a checksummed cache, then hand back a reader.

    Qlib is asked for a bounded batch of securities at a time, so its expression
    engine never holds the whole market at once. A caller running many models over
    one dataset -- a parameter search being the obvious one -- pays this once.
    """
    identity = cache_identity(provider, feature_set, spec, snapshot, policy_fingerprint)
    root = cache_root / identity[:20]
    mature = frozenset(
        mature_label_dates(
            provider.manifest.calendar, snapshot.coverage_end, spec.label_horizon
        )
    )
    with exclusive_lock(root):
        try:
            # `verified` reports a corrupt or foreign artifact as ValueError, at
            # the root and at each shard. Left alone either escapes as an
            # unhandled error rather than the data error a caller is promised,
            # so the whole assembly is covered rather than the first check.
            if not verified(root, identity):
                _write_shards(provider, feature_set, spec, universe, root, identity)
            covered = read_json(root / "covered.json")
        except (OSError, ValueError) as exc:
            raise FeatureCacheError(str(exc)) from exc
    held = frozenset(date.fromisoformat(day) for day in covered["dates"])
    missing = sorted(set(required_days) - held)
    if missing:
        raise FeatureCacheError(
            f"The feature cache is missing {len(missing)} of the {len(set(required_days))} "
            f"trading days this run needs, first {missing[0]}: it was built for a "
            "different set of days and would train on a silently short panel"
        )
    return ShardedDataset(
        root=root,
        feature_names=tuple(feature_set.column_names),
        label_horizon=spec.label_horizon,
        scan_batch_rows=spec.scan_batch_rows,
        mature_dates=mature,
        covered_dates=held,
    )


def _write_shards(provider, feature_set, spec, universe, root: Path, identity: str) -> None:
    securities = sorted(set().union(*universe.values())) if universe else []
    if not securities:
        raise FeatureCacheError("The universe is empty over the requested span")
    size = spec.feature_batch_size
    batches = [securities[index : index + size] for index in range(0, len(securities), size)]
    plan = {
        "identity": identity,
        "securities": securities,
        "batches": [
            {"batch_id": f"batch-{number:04d}", "securities": members}
            for number, members in enumerate(batches, 1)
        ],
    }
    existing = root / "shard-plan.json"
    if existing.exists() and read_json(existing) != plan:
        raise FeatureCacheError("Feature shard plan changed under the same identity")
    write_json(existing, plan)
    fields = list(dict.fromkeys([*feature_set.expressions, "$close"]))
    for batch in plan["batches"]:
        batch_root = root / batch["batch_id"]
        batch_identity = digest([identity, batch])
        if verified(batch_root, batch_identity):
            continue
        try:
            raw = read_features(
                provider.path,
                instruments=batch["securities"],
                fields=fields,
                start=provider.manifest.coverage_start.isoformat(),
                end=provider.manifest.coverage_end.isoformat(),
                kernels=spec.qlib_kernels,
            )
        except Exception as exc:
            raise FeatureCacheError(f"Qlib feature read failed: {exc}") from exc
        if raw.empty:
            raise FeatureCacheError(f"Qlib returned no rows for {batch['batch_id']}")
        # Restrict before copying hundreds of columns. The untouched close series is
        # still used below: a forward label reaches past the pool membership.
        restricted = restrict_to_universe(raw, universe)
        features = (
            restricted[list(feature_set.expressions)]
            .replace([np.inf, -np.inf], np.nan)  # InfToNaN
            .copy()
        )
        features.columns = list(feature_set.column_names)
        features = features.swaplevel().sort_index()
        features.index = features.index.set_names(["datetime", "instrument"])
        frame = features.reset_index()
        frame["instrument"] = frame["instrument"].astype(str)
        write_parquet(batch_root / "features.parquet", frame)

        grouped_close = raw["$close"].groupby(level="instrument")
        forward = grouped_close.shift(-(spec.label_horizon + 1)) / grouped_close.shift(-1) - 1
        forward = forward.swaplevel().sort_index()
        forward.index = forward.index.set_names(["datetime", "instrument"])
        labels = frame[["datetime", "instrument"]].copy()
        labels[f"raw_h{spec.label_horizon}"] = forward.reindex(features.index).to_numpy()
        write_parquet(batch_root / "raw-labels.parquet", labels)
        del raw, restricted, features, frame, labels
        seal(batch_root, batch_identity)

    parts = [
        pd.read_parquet(root / batch["batch_id"] / "raw-labels.parquet")
        for batch in plan["batches"]
    ]
    labels = pd.concat(parts, ignore_index=True)
    del parts
    if labels.duplicated(["datetime", "instrument"]).any():
        raise FeatureCacheError("Duplicate labels across feature shards")
    source = f"raw_h{spec.label_horizon}"
    # CSRankNorm(label): rank within each cross-section, centre, scale by Qlib's
    # constant. Stateless, so precomputing it fits no window and leaks nothing; a
    # NaN label stays NaN and is dropped when a learning segment is loaded.
    labels[f"learn_h{spec.label_horizon}"] = (
        labels.groupby("datetime", group_keys=False)[source].rank(pct=True) - 0.5
    ) * 3.46
    labels = labels.sort_values(["datetime", "instrument"], kind="stable")
    write_parquet(root / "labels.parquet", labels)
    # Every day the pool named must have produced a row for every member it
    # named. Checking only that the day appears misses the case this is really
    # guarding: Qlib answering for part of a batch leaves features and labels
    # short together, so nothing downstream can notice, and the train and valid
    # segments carry no coverage threshold to catch it later.
    written = pd.to_datetime(labels["datetime"]).dt.date.value_counts()
    short = {
        day: (int(written.get(day, 0)), len(members))
        for day, members in universe.items()
        if int(written.get(day, 0)) != len(members)
    }
    if short:
        first = min(short)
        got, wanted = short[first]
        raise FeatureCacheError(
            f"{len(short)} of {len(universe)} days hold fewer rows than the stock pool "
            f"names, first {first} with {got} of {wanted}: the provider answered for "
            "part of the universe and a shorter panel would train without complaint"
        )
    write_json(root / "covered.json", {
        "schema": FEATURE_CACHE_SCHEMA_VERSION,
        "dates": [day.isoformat() for day in sorted(universe)],
        "members": {day.isoformat(): len(members) for day, members in sorted(universe.items())},
        "rows": int(len(labels)),
    })
    seal(root, identity)


