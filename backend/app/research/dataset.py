"""Assembling the feature/label matrix a model trains on.

The features are Qlib expressions and are evaluated by Qlib. The label is not:
"the open after this weekly cross-section to the open after the next one"
depends on which days were chosen as rebalance dates, which is a property of the
experiment rather than of the price series, and no `$`-expression can express
it. Ticket 06 already computes it, so it is computed there and joined here.

`StaticDataLoader` is what makes the join legitimate rather than a workaround: it
is Qlib's own entry point for a frame that was built elsewhere, so the result
still flows through `DataHandlerLP` and picks up the processors, the fit window
and the learn/infer split exactly as an Alpha158 handler would.

Two rules are enforced while the frame is built, both of them about what must
*not* be in it:

- a row exists only if its security was in the frozen `ResearchUniverse` of that
  cross-section, so a security that entered the pool later cannot contribute a
  training row it was never eligible for;
- feature values and label values are read in separate passes with separate date
  bounds, so the code cannot accidentally let a label column reach the feature
  matrix.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.core.logging import get_logger
from app.research.execution_spec import ModelExecutionSpec
from app.research.labels import calculate_weekly_labels
from app.research.processors import InfToNaN
from app.research.qlib_runtime import read_features

logger = get_logger(__name__)

#: Qlib's conventional single-label column name; `SignalRecord` and
#: `SigAnaRecord` both assume it.
LABEL_COLUMN = "LABEL0"
MISSING_RATE_DRIFT_THRESHOLD = 0.10


@dataclass(frozen=True)
class AssembledDataset:
    """The frame plus what is needed to explain and audit it."""

    handler: Any
    dataset: Any
    #: (segment, feature name) -> fraction of rows that are NaN. Published as a
    #: diagnostic because nothing is imputed: a column that is 3% missing in
    #: train and 40% missing in test is a data problem wearing a model's
    #: clothes, and only this table shows it.
    missing_rate: pd.DataFrame
    #: Full label audit, including rows whose future outcome is not mature or
    #: whose entry/exit price is structurally missing.
    labels: pd.DataFrame
    #: Root-cause counts for current values that invalidate a whole Alpha360
    #: lag group, kept separate from the per-column missing-rate table.
    feature_group_anomalies: pd.DataFrame
    feature_rows: int
    label_rows: int
    #: The window declared to `check_transform_proc`. Carried explicitly because
    #: `DataHandlerLP` does not keep it: it only reaches processors that ask for
    #: it, and none currently do.
    fit_window: tuple[date, date]


def build_dataset(
    *,
    bundle_path: Path,
    spec: ModelExecutionSpec,
    universe: dict[date, set[str]],
    weekly_observations: Sequence[date],
    calendar: Sequence[date],
) -> AssembledDataset:
    """Read features, attach labels, and wrap the result in a Qlib dataset."""
    from qlib.contrib.data.handler import check_transform_proc
    from qlib.data.dataset import DatasetH
    from qlib.data.dataset.handler import DataHandlerLP
    from qlib.data.dataset.loader import StaticDataLoader

    feature_set = spec.feature_set
    # `$open` rides along with the feature expressions so the label can be built
    # from the same read rather than a second pass over the bundle.
    fields = list(dict.fromkeys([*feature_set.expressions, "$open", "$close", "$volume"]))
    raw = read_features(
        bundle_path,
        instruments="all",
        fields=fields,
        start=calendar[0].isoformat(),
        end=calendar[-1].isoformat(),
    )

    opens = _matrix(raw, "$open")
    labels = _restrict_label_rows(
        calculate_weekly_labels(opens, weekly_observation_dates=list(weekly_observations)),
        universe,
    )

    features = raw[list(feature_set.expressions)].copy()
    features.columns = list(feature_set.column_names)
    # `D.features` indexes by (instrument, datetime); every handler and
    # processor downstream groups by datetime first, so the levels are swapped
    # once here rather than defended against in each of them.
    features = features.swaplevel().sort_index()
    features = _restrict_to_universe(features, universe)

    current_values = raw[["$close", "$volume"]].copy().swaplevel().sort_index()
    current_values = _restrict_to_universe(current_values, universe)

    label_frame = (
        labels.loc[labels["label"].notna(), ["observation_date", "instrument_id", "label"]]
        .rename(columns={"label": LABEL_COLUMN})
        .set_index(
            pd.MultiIndex.from_arrays(
                [
                    pd.to_datetime(labels.loc[labels["label"].notna(), "observation_date"]),
                    labels.loc[labels["label"].notna(), "instrument_id"],
                ],
                names=["datetime", "instrument"],
            )
        )[[LABEL_COLUMN]]
    )
    label_frame = _restrict_to_universe(label_frame, universe)

    missing_rate = _missing_rate(features, spec)
    feature_group_anomalies = _feature_group_anomalies(current_values, spec)

    # `check_transform_proc` is Qlib's own mechanism for "this processor may
    # only fit on the training range": it injects the window into any processor
    # whose constructor declares `fit_start_time`/`fit_end_time`. None of ours
    # do — they are all stateless — so today it is a no-op. It is used anyway
    # because the first fitted processor someone adds (a linear model needs one)
    # then gets the window automatically instead of silently estimating its
    # parameters over the whole range.
    #
    # Qlib's own processors go in as config dicts so that injection can reach
    # them; `InfToNaN` goes in as an instance because it is ours and resolving
    # it by name would mean handing a module path to `init_instance_by_config`.
    infer_processors = check_transform_proc(
        [InfToNaN(fields_group="feature")], spec.fit_start, spec.fit_end
    )
    learn_processors = check_transform_proc(
        [
            {"class": "DropnaLabel"},
            {"class": "CSRankNorm", "kwargs": {"fields_group": "label"}},
        ],
        spec.fit_start,
        spec.fit_end,
    )
    handler = DataHandlerLP(
        instruments=None,
        data_loader=StaticDataLoader(config={"feature": features, "label": label_frame}),
        infer_processors=infer_processors,
        learn_processors=learn_processors,
        process_type=DataHandlerLP.PTYPE_A,
    )
    dataset = DatasetH(
        handler=handler,
        segments={
            "train": (spec.split.train.start, spec.split.train.end),
            "valid": (spec.split.valid.start, spec.split.valid.end),
            "test": (spec.split.test.start, spec.split.test.end),
        },
    )
    logger.info(
        "model_dataset.assembled",
        feature_columns=len(feature_set.features),
        feature_rows=len(features),
        label_rows=len(label_frame),
        weekly_observations=len(weekly_observations),
    )
    return AssembledDataset(
        handler=handler,
        dataset=dataset,
        missing_rate=missing_rate,
        labels=labels,
        feature_group_anomalies=feature_group_anomalies,
        feature_rows=len(features),
        label_rows=len(label_frame),
        fit_window=(spec.fit_start, spec.fit_end),
    )


def _matrix(frame: pd.DataFrame, field: str) -> pd.DataFrame:
    matrix = frame[field].unstack(level="instrument")
    matrix.index = pd.Index(item.date() for item in matrix.index)
    matrix.columns = matrix.columns.map(str)
    return matrix


def _restrict_to_universe(frame: pd.DataFrame, universe: dict[date, set[str]]) -> pd.DataFrame:
    """Drop every row whose security was not in the pool on that day.

    Filtering here rather than after training is the point: a security admitted
    to the pool in March must not contribute a January training row. Doing it
    later would leave the model already fitted on rows it was never entitled to.
    """
    if frame.empty:
        return frame
    dates = frame.index.get_level_values("datetime")
    instruments = frame.index.get_level_values("instrument")
    keep = [
        instrument in universe.get(stamp.date(), ())
        for stamp, instrument in zip(dates, instruments)
    ]
    return frame.loc[keep]


def _restrict_label_rows(
    labels: pd.DataFrame,
    universe: dict[date, set[str]],
) -> pd.DataFrame:
    keep = [
        instrument_id in universe.get(observation_date, ())
        for observation_date, instrument_id in zip(
            labels["observation_date"], labels["instrument_id"]
        )
    ]
    return labels.loc[keep].reset_index(drop=True)


def _missing_rate(features: pd.DataFrame, spec: ModelExecutionSpec) -> pd.DataFrame:
    rows: list[dict] = []
    stamps = features.index.get_level_values("datetime")
    for segment in spec.split.segments:
        window = features.loc[
            (stamps >= pd.Timestamp(segment.start)) & (stamps <= pd.Timestamp(segment.end))
        ]
        if window.empty:
            continue
        fractions = window.isna().mean()
        rows.extend(
            {
                "segment": segment.name,
                "feature_name": name,
                "missing_rate": float(value),
                "rows": len(window),
            }
            for name, value in fractions.items()
        )
    return pd.DataFrame(rows)


def _feature_group_anomalies(
    current_values: pd.DataFrame,
    spec: ModelExecutionSpec,
) -> pd.DataFrame:
    """Count root-cause rows that invalidate an Alpha360 lag group at once."""
    rows: list[dict] = []
    stamps = current_values.index.get_level_values("datetime")
    groups = (("price_lags", "$close"), ("volume_lags", "$volume"))
    for segment in spec.split.segments:
        window = current_values.loc[
            (stamps >= pd.Timestamp(segment.start)) & (stamps <= pd.Timestamp(segment.end))
        ]
        for feature_group, source_field in groups:
            values = pd.to_numeric(window[source_field], errors="coerce")
            invalid = values.isna() | ~np.isfinite(values) | (values <= 0)
            rows.append(
                {
                    "segment": segment.name,
                    "feature_group": feature_group,
                    "source_field": source_field.removeprefix("$"),
                    "invalid_rows": int(invalid.sum()),
                    "rows": len(window),
                }
            )
    return pd.DataFrame(rows)


def missing_rate_drift(
    missing_rate: pd.DataFrame,
    *,
    threshold: float = MISSING_RATE_DRIFT_THRESHOLD,
) -> pd.DataFrame:
    """Columns whose test missing rate exceeds train by the warning threshold."""
    columns = ["feature_name", "train_missing_rate", "test_missing_rate", "drift"]
    if missing_rate.empty:
        return pd.DataFrame(columns=columns)
    pivot = missing_rate.pivot(index="feature_name", columns="segment", values="missing_rate")
    if not {"train", "test"} <= set(pivot.columns):
        return pd.DataFrame(columns=columns)
    drift = (pivot["test"] - pivot["train"]).rename("drift")
    result = pivot.assign(drift=drift).loc[drift > threshold, ["train", "test", "drift"]]
    return result.rename(
        columns={"train": "train_missing_rate", "test": "test_missing_rate"}
    ).reset_index()
