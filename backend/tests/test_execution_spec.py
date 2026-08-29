"""Refusing to run an old experiment with new code.

The scenario is a deploy landing between a run being queued and the worker
picking it up. `create_run` recomputes the fingerprint from current code, so a
changed registry normally produces a *new* experiment rather than reusing an old
one — but a run already queued keeps pointing at the experiment it was created
against, and a single serial worker can leave it queued for as long as a bundle
build takes.

The alternative to refusing is worse in both directions: run today's code and
file the result under yesterday's definition, or execute the expressions stored
in the database and hand user-reachable strings to Qlib's class resolver.
"""

from __future__ import annotations

import copy
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.research.execution_spec import (
    ERROR_CODE,
    ExecutionDefinitionUnavailable,
    compile_execution_spec,
)
from app.research.feature_sets import get_feature_set
from app.research.model_definition import (
    DEFAULT_SEED,
    ModelResearchDefinition,
    resolve_model_params,
)
from app.research.splits import build_split_plan, propose_split


def _calendar(weeks: int = 30) -> tuple[list[date], list[date]]:
    start = date(2025, 1, 6)
    sessions = [
        start + timedelta(days=offset)
        for offset in range(weeks * 7)
        if (start + timedelta(days=offset)).weekday() < 5
    ]
    return sessions, [item for item in sessions if item.weekday() == 4]


def _definition() -> ModelResearchDefinition:
    sessions, weekly = _calendar()
    return ModelResearchDefinition(
        data_snapshot_id=uuid4(),
        feature_set=get_feature_set("alpha158_jp_v1"),
        split=build_split_plan(
            weekly_observations=weekly, calendar=sessions, **propose_split(weekly)
        ),
        stock_pool_policy_fingerprint="abc123",
        model_params=resolve_model_params(None, seed=DEFAULT_SEED),
    )


def _stored(definition: ModelResearchDefinition, **overrides):
    """A stand-in for the `research_experiments` row.

    A namespace rather than the ORM model: nothing here touches the database,
    and the compile step reads exactly three attributes.
    """
    payload = copy.deepcopy(definition.canonical_payload)
    return SimpleNamespace(
        id=uuid4(),
        data_snapshot_id=definition.data_snapshot_id,
        definition=payload,
        definition_fingerprint=definition.fingerprint,
        **overrides,
    )


def test_an_unchanged_experiment_compiles() -> None:
    definition = _definition()

    spec = compile_execution_spec(_stored(definition))

    assert spec.feature_set.name == "alpha158_jp_v1"
    assert spec.fit_start == definition.split.train.start
    assert spec.fit_end == definition.split.train.end
    assert spec.seed == DEFAULT_SEED


def test_the_compiled_feature_set_comes_from_code_not_from_storage() -> None:
    """The expressions handed to Qlib must be module-level constants.

    Storage is compared against, never executed — so a tampered expression is a
    refusal, not an instruction.
    """
    definition = _definition()
    experiment = _stored(definition)
    experiment.definition["feature_set"]["columns"][0]["expression"] = "__import__('os').system('x')"

    with pytest.raises(ExecutionDefinitionUnavailable, match="feature_set differs"):
        compile_execution_spec(experiment)


def test_an_edited_processor_semantics_version_is_refused() -> None:
    definition = _definition()
    experiment = _stored(definition)
    experiment.definition["processors"]["infer"][0]["semantics_version"] += 1

    with pytest.raises(ExecutionDefinitionUnavailable, match="processors differs"):
        compile_execution_spec(experiment)


def test_a_feature_set_that_no_longer_exists_is_refused() -> None:
    definition = _definition()
    experiment = _stored(definition)
    experiment.definition["feature_set"]["name"] = "alpha999_removed"

    with pytest.raises(ExecutionDefinitionUnavailable, match="no longer registered"):
        compile_execution_spec(experiment)


def test_a_fit_window_that_disagrees_with_the_train_segment_is_refused() -> None:
    """The window that governs where a fitted processor may look.

    Nothing fits today, so nothing would notice — which is precisely why it is
    checked here rather than left to the first processor that does.
    """
    definition = _definition()
    experiment = _stored(definition)
    experiment.definition["fit_window"]["fit_end_time"] = definition.split.test.end.isoformat()

    with pytest.raises(ExecutionDefinitionUnavailable, match="does not match the train segment"):
        compile_execution_spec(experiment)


def test_a_changed_label_definition_is_refused() -> None:
    definition = _definition()
    experiment = _stored(definition)
    experiment.definition["label"]["weekly"] = "close_to_close"

    with pytest.raises(ExecutionDefinitionUnavailable, match="label.weekly"):
        compile_execution_spec(experiment)


def test_a_definition_that_no_longer_hashes_to_its_fingerprint_is_refused() -> None:
    """The backstop for anything the field-by-field comparison does not know about.

    Here the model parameters were edited in place — no comparison above looks
    at them, but the recompiled fingerprint does not match.
    """
    definition = _definition()
    experiment = _stored(definition)
    experiment.definition["model_params"]["learning_rate"] = 0.99

    with pytest.raises(ExecutionDefinitionUnavailable, match="hashes to"):
        compile_execution_spec(experiment)


def test_the_refusal_carries_the_documented_error_code() -> None:
    definition = _definition()
    experiment = _stored(definition)
    experiment.definition["label"]["weekly"] = "close_to_close"

    with pytest.raises(ExecutionDefinitionUnavailable) as caught:
        compile_execution_spec(experiment)

    assert caught.value.error_code == ERROR_CODE
    assert caught.value.differences


def test_the_inference_contract_is_self_contained() -> None:
    """Ticket 12 must not have to walk run -> experiment -> manifest to score."""
    spec = compile_execution_spec(_stored(_definition()))

    contract = spec.inference_contract

    assert contract["feature_set"]["columns"]
    assert contract["processors"]["infer"][0]["class"] == "InfToNaN"
    assert contract["label"]["weekly"]
    assert contract["fit_window"]["fit_start_time"]
    assert contract["model_params"]["deterministic"] is True
