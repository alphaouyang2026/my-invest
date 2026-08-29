"""Compiling an experiment into something Qlib can run, without trusting the database.

Two rules meet here and appear to contradict each other.

An experiment is immutable: its expanded feature definitions, processor
identities, splits and parameters are frozen in `research_experiments.definition`
so a rerun means what the original run meant.

Nothing from the database may reach Qlib: `init_instance_by_config` resolves a
class by importing the module path it is handed, so an expression or class name
loaded from a column would be arbitrary code execution — 06 forbade it and 07
inherits the ban.

Executing the stored definition satisfies the first rule and breaks the second.
Executing the current registry satisfies the second and breaks the first: a
worker that starts after a deploy would build today's features and file the
result under an experiment whose stored definition describes yesterday's.

The way out is not to choose. The spec is **compiled from the trusted registry**
and then **compared field by field with the stored definition**. Matching means
the code that is about to run is the code the experiment was created with, and
only the compiled objects — never the stored strings — are handed to Qlib. A
mismatch is refused before `qlib.init` is called.

The window this closes is narrow but real: `create_run` recomputes the
fingerprint from current code, so a changed registry produces a *new* experiment
rather than reusing an old one. But a run already queued against an experiment
keeps its reference across a deploy, and a single serial worker can leave it
queued for as long as a bundle build takes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from app.core.logging import get_logger
from app.models.research import ResearchExperiment
from app.research.feature_sets import FeatureSet, UnknownFeatureSetError, get_feature_set
from app.research.model_definition import (
    ModelResearchDefinition,
    WEEKLY_LABEL_DEFINITION,
    processor_payload,
)
from app.research.splits import Segment, SplitPlan

logger = get_logger(__name__)

ERROR_CODE = "experiment_execution_definition_unavailable"


class ExecutionDefinitionUnavailable(RuntimeError):
    """The current code cannot reproduce this experiment's definition.

    Raised before Qlib is initialised and before any bundle is read, so a run
    that cannot mean what it claims never gets far enough to publish anything.
    """

    error_code = ERROR_CODE

    def __init__(self, differences: list[str]) -> None:
        self.differences = differences
        joined = "; ".join(differences)
        super().__init__(
            f"Stored experiment definition does not match the current registry: {joined}"
        )


@dataclass(frozen=True)
class ModelExecutionSpec:
    """Everything the worker needs, all of it built from code.

    The `FeatureSet` here is the registry object, so the expressions handed to
    Qlib are module-level constants. `stored_definition` is carried only for
    logging and for writing the inference contract — it is never executed.
    """

    experiment_id: Any
    data_snapshot_id: Any
    feature_set: FeatureSet
    split: SplitPlan
    model_params: dict[str, Any]
    seed: int
    processors: dict[str, list[dict[str, Any]]]
    fit_start: date
    fit_end: date
    stored_definition: dict[str, Any]

    @property
    def label_definition(self) -> str:
        return WEEKLY_LABEL_DEFINITION

    @property
    def required_history_days(self) -> int:
        return max(self.feature_set.max_window, 147)

    @property
    def inference_contract(self) -> dict[str, Any]:
        """What ticket 12 needs to score a new snapshot with this model.

        Deliberately self-contained: without it, reusing a model means walking
        `TrainedModel -> ResearchRun -> ResearchExperiment -> artifact manifest`
        and hoping every hop still resolves.
        """
        return {
            "feature_set": self.feature_set.definition_payload,
            "processors": self.processors,
            "label": {"weekly": self.label_definition},
            "fit_window": {
                "fit_start_time": self.fit_start.isoformat(),
                "fit_end_time": self.fit_end.isoformat(),
            },
            "model_params": self.model_params,
        }


def compile_execution_spec(experiment: ResearchExperiment) -> ModelExecutionSpec:
    """Rebuild the experiment from trusted code and prove it still matches."""
    stored = dict(experiment.definition or {})
    differences: list[str] = []

    feature_set = _resolve_feature_set(stored, differences)
    split = _rebuild_split(stored, differences)
    if feature_set is None or split is None:
        raise ExecutionDefinitionUnavailable(differences)

    current_processors = processor_payload()
    _compare("processors", stored.get("processors"), current_processors, differences)
    _compare(
        "feature_set",
        stored.get("feature_set"),
        feature_set.definition_payload,
        differences,
    )

    model_params = dict(stored.get("model_params") or {})
    seed = model_params.get("seed")
    if not isinstance(seed, int):
        differences.append("model_params.seed is missing or not an integer")

    fit_window = stored.get("fit_window") or {}
    fit_start = _parse_date(fit_window.get("fit_start_time"))
    fit_end = _parse_date(fit_window.get("fit_end_time"))
    if fit_start is None or fit_end is None:
        differences.append("fit_window is missing or unparseable")
    elif (fit_start, fit_end) != (split.train.start, split.train.end):
        differences.append(
            f"fit_window {fit_start}..{fit_end} does not match the train segment "
            f"{split.train.start}..{split.train.end}"
        )

    label = (stored.get("label") or {}).get("weekly")
    if label != WEEKLY_LABEL_DEFINITION:
        differences.append(f"label.weekly is {label!r}, current code defines {WEEKLY_LABEL_DEFINITION!r}")

    if differences:
        logger.warning(
            "model_execution_spec.rejected",
            experiment_id=str(experiment.id),
            differences=differences,
        )
        raise ExecutionDefinitionUnavailable(differences)

    # The compiled definition must reproduce the fingerprint the experiment was
    # stored under. Everything above compares fields; this catches anything the
    # comparison does not know to look at.
    recompiled = ModelResearchDefinition(
        data_snapshot_id=experiment.data_snapshot_id,
        feature_set=feature_set,
        split=split,
        stock_pool_policy_fingerprint=stored.get("stock_pool_policy_fingerprint", ""),
        model_params=model_params,
        seed=seed,
        processors=current_processors,
    )
    if recompiled.fingerprint != experiment.definition_fingerprint:
        raise ExecutionDefinitionUnavailable(
            [
                "recompiled definition hashes to "
                f"{recompiled.fingerprint[:12]}… but the experiment is stored under "
                f"{experiment.definition_fingerprint[:12]}…"
            ]
        )

    logger.info(
        "model_execution_spec.compiled",
        experiment_id=str(experiment.id),
        feature_set=feature_set.name,
        columns=len(feature_set.features),
    )
    return ModelExecutionSpec(
        experiment_id=experiment.id,
        data_snapshot_id=experiment.data_snapshot_id,
        feature_set=feature_set,
        split=split,
        model_params=model_params,
        seed=seed,
        processors=current_processors,
        fit_start=split.train.start,
        fit_end=split.train.end,
        stored_definition=stored,
    )


def _resolve_feature_set(stored: dict, differences: list[str]) -> FeatureSet | None:
    name = (stored.get("feature_set") or {}).get("name")
    if not name:
        differences.append("feature_set.name is missing from the stored definition")
        return None
    try:
        return get_feature_set(name)
    except UnknownFeatureSetError:
        # The set was removed or renamed since the experiment was created. The
        # stored expressions could technically be run, but that is exactly the
        # database-to-Qlib path this module exists to forbid.
        differences.append(f"feature set {name!r} is no longer registered")
        return None


def _rebuild_split(stored: dict, differences: list[str]) -> SplitPlan | None:
    splits = stored.get("splits") or {}
    try:
        segments = {
            name: Segment(
                name=name,
                start=date.fromisoformat(splits[name]["start"]),
                end=date.fromisoformat(splits[name]["end"]),
                # Observation dates are not stored per segment — only the count.
                # They are re-derived from the calendar at run time; the payload
                # comparison below is what keeps the two in step.
                observations=(),
            )
            for name in ("train", "valid", "test")
        }
        embargo = tuple(date.fromisoformat(item) for item in splits.get("embargo", []))
    except (KeyError, TypeError, ValueError) as exc:
        differences.append(f"splits are missing or unparseable ({exc})")
        return None
    return SplitPlan(
        train=segments["train"], valid=segments["valid"], test=segments["test"], embargo=embargo
    )


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _compare(label: str, stored: Any, current: Any, differences: list[str]) -> None:
    if stored == current:
        return
    differences.append(f"{label} differs from the current registry")
    logger.debug("model_execution_spec.field_mismatch", field=label, stored=stored, current=current)
