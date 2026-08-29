"""Identity and admission rules for a model experiment.

Two things are under test and they are easy to confuse. The *whitelist* decides
which requests are allowed in at all. The *fingerprint* decides whether two
allowed requests mean the same research. A gap in the first lets a caller void
the reproducibility guarantee; a gap in the second lets two runs of different
meaning share one `ResearchExperiment`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from uuid import uuid4

import pytest

from app.research.feature_sets import get_feature_set
from app.research.model_definition import (
    DEFAULT_SEED,
    LOCKED_PARAMS,
    ModelResearchDefinition,
    ParameterError,
    processor_payload,
    resolve_model_params,
)
from app.research.splits import SplitError, build_split_plan, label_exit_session, propose_split


def _calendar(weeks: int = 30) -> tuple[list[date], list[date]]:
    start = date(2025, 1, 6)
    sessions = [
        start + timedelta(days=offset)
        for offset in range(weeks * 7)
        if (start + timedelta(days=offset)).weekday() < 5
    ]
    return sessions, [item for item in sessions if item.weekday() == 4]


def _definition(**overrides) -> ModelResearchDefinition:
    sessions, weekly = _calendar()
    plan = build_split_plan(
        weekly_observations=weekly, calendar=sessions, **propose_split(weekly)
    )
    base = {
        "data_snapshot_id": uuid4(),
        "feature_set": get_feature_set("alpha158_jp_v1"),
        "split": plan,
        "stock_pool_policy_fingerprint": "abc123",
        "model_params": resolve_model_params(None, seed=DEFAULT_SEED),
    }
    return ModelResearchDefinition(**{**base, **overrides})


# --------------------------------------------------------------------------
# Parameter whitelist
# --------------------------------------------------------------------------


def test_defaults_expand_one_seed_into_four_and_apply_locked_values() -> None:
    params = resolve_model_params(None, seed=7)

    assert params["seed"] == params["bagging_seed"] == 7
    assert params["feature_fraction_seed"] == params["data_random_seed"] == 7
    for key, value in LOCKED_PARAMS.items():
        assert params[key] == value
    # A resource setting, not part of the model definition.
    assert "num_threads" not in params


@pytest.mark.parametrize("key", ["objective", "deterministic", "force_row_wise"])
def test_locked_parameters_cannot_be_overridden(key: str) -> None:
    with pytest.raises(ParameterError, match="fixed by the design"):
        resolve_model_params({key: 0}, seed=1)


@pytest.mark.parametrize("key", ["seed", "bagging_seed", "feature_fraction_seed", "data_random_seed"])
def test_seed_has_exactly_one_entry_point(key: str) -> None:
    """Two entry points would need a precedence rule; the request is refused instead."""
    with pytest.raises(ParameterError, match="top-level 'seed' field"):
        resolve_model_params({key: 99}, seed=42)


def test_unknown_parameters_are_rejected_rather_than_ignored() -> None:
    with pytest.raises(ParameterError, match="Unknown model parameter"):
        resolve_model_params({"boosting": "dart"}, seed=1)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("learning_rate", 0.0),      # exclusive lower bound
        ("learning_rate", 0.6),
        ("num_leaves", 1),
        ("num_leaves", 256),
        ("num_boost_round", 5001),   # the resource ceiling
        ("max_depth", 3.5),          # non-integer for an integer parameter
    ],
)
def test_out_of_range_values_are_rejected_not_clamped(key: str, value: float) -> None:
    with pytest.raises(ParameterError):
        resolve_model_params({key: value}, seed=1)


def test_accepted_override_reaches_the_resolved_parameters() -> None:
    params = resolve_model_params({"learning_rate": 0.03, "num_leaves": 15}, seed=1)

    assert params["learning_rate"] == 0.03
    assert params["num_leaves"] == 15


# --------------------------------------------------------------------------
# Fingerprint — semantic change detection
# --------------------------------------------------------------------------


def test_identical_definitions_share_a_fingerprint() -> None:
    snapshot = uuid4()
    assert _definition(data_snapshot_id=snapshot).fingerprint == _definition(
        data_snapshot_id=snapshot
    ).fingerprint


def test_editing_a_feature_expression_changes_the_fingerprint() -> None:
    """The protection a name-plus-version scheme cannot give.

    Someone edits an expression and forgets to bump the version; the expanded
    definition moves the fingerprint anyway.
    """
    original = _definition()
    feature_set = original.feature_set
    edited_column = replace(
        feature_set.features[0], expression=feature_set.features[0].expression + "*1.0"
    )
    edited = replace(feature_set, features=(edited_column,) + feature_set.features[1:])

    assert _definition(
        data_snapshot_id=original.data_snapshot_id, feature_set=edited
    ).fingerprint != original.fingerprint


def test_choosing_a_different_feature_set_changes_the_fingerprint() -> None:
    original = _definition()
    other = _definition(
        data_snapshot_id=original.data_snapshot_id,
        feature_set=get_feature_set("alpha360_jp_v1"),
    )

    assert other.fingerprint != original.fingerprint


def test_changing_the_processor_semantics_version_changes_the_fingerprint() -> None:
    """The only guard on `InfToNaN`'s manual version number.

    Class name and kwargs stay identical when its implementation changes, so
    without this the two behaviours would reuse one experiment.
    """
    original = _definition()
    bumped = processor_payload()
    bumped["infer"][0]["semantics_version"] += 1

    assert _definition(
        data_snapshot_id=original.data_snapshot_id, processors=bumped
    ).fingerprint != original.fingerprint


def test_changing_the_fit_window_changes_the_fingerprint() -> None:
    original = _definition()
    sessions, weekly = _calendar()
    proposed = propose_split(weekly)
    # Shorten train by one cross-section; the fit window moves with it.
    shorter = dict(proposed, train_start=weekly[1])
    moved = _definition(
        data_snapshot_id=original.data_snapshot_id,
        split=build_split_plan(weekly_observations=weekly, calendar=sessions, **shorter),
    )

    assert moved.fit_window != original.fit_window
    assert moved.fingerprint != original.fingerprint


def test_seed_changes_the_fingerprint_but_thread_count_is_absent() -> None:
    original = _definition()
    reseeded = _definition(
        data_snapshot_id=original.data_snapshot_id,
        model_params=resolve_model_params(None, seed=DEFAULT_SEED + 1),
    )

    assert reseeded.fingerprint != original.fingerprint
    assert "num_threads" not in original.canonical_payload["model_params"]


def test_fingerprint_is_stable_under_key_reordering() -> None:
    """Canonicalisation has to happen before hashing.

    Otherwise the same research would land in a different experiment depending
    on dictionary insertion order.
    """
    original = _definition()
    shuffled = {key: original.model_params[key] for key in reversed(list(original.model_params))}

    assert _definition(
        data_snapshot_id=original.data_snapshot_id, model_params=shuffled
    ).fingerprint == original.fingerprint


def test_fit_window_is_the_train_segment() -> None:
    definition = _definition()

    assert definition.fit_window == {
        "fit_start_time": definition.split.train.start.isoformat(),
        "fit_end_time": definition.split.train.end.isoformat(),
    }


def test_required_history_covers_the_momentum_control_even_for_short_windows() -> None:
    """Alpha158's longest window is 60, but the 6-1 control still needs 147."""
    assert _definition(feature_set=get_feature_set("alpha158_jp_v1")).required_history_days == 147


# --------------------------------------------------------------------------
# Split isolation
# --------------------------------------------------------------------------


def test_proposed_split_reserves_two_embargo_cross_sections() -> None:
    sessions, weekly = _calendar()
    plan = build_split_plan(
        weekly_observations=weekly, calendar=sessions, **propose_split(weekly)
    )

    assert len(plan.embargo) == 2
    claimed = {item for segment in plan.segments for item in segment.observations}
    assert claimed.isdisjoint(plan.embargo)
    assert len(claimed) + len(plan.embargo) == len(weekly)


def test_a_one_cross_section_gap_is_rejected_at_both_seams() -> None:
    """The off-by-one the design used to permit.

    `train_end` and `valid_start` sit a week apart, yet the label of `train_end`
    only resolves after `valid_start`.
    """
    sessions, weekly = _calendar()
    proposed = propose_split(weekly)
    index = weekly.index(proposed["train_end"])

    with pytest.raises(SplitError, match="Label leakage across the train/valid seam"):
        build_split_plan(
            weekly_observations=weekly,
            calendar=sessions,
            **dict(proposed, valid_start=weekly[index + 1]),
        )


def test_valid_test_seam_is_checked_too() -> None:
    """The seam a label reaches through early stopping rather than training loss."""
    sessions, weekly = _calendar()
    proposed = propose_split(weekly)
    index = weekly.index(proposed["valid_end"])

    with pytest.raises(SplitError, match="Label leakage across the valid/test seam"):
        build_split_plan(
            weekly_observations=weekly,
            calendar=sessions,
            **dict(proposed, test_start=weekly[index + 1]),
        )


def test_label_exit_falls_before_the_next_segments_feature_cutoff() -> None:
    sessions, weekly = _calendar()
    plan = build_split_plan(
        weekly_observations=weekly, calendar=sessions, **propose_split(weekly)
    )

    for earlier, later in ((plan.train, plan.valid), (plan.valid, plan.test)):
        exit_session = label_exit_session(
            earlier.observations[-1], weekly_observations=weekly, calendar=sessions
        )
        assert exit_session < later.observations[0]


def test_segments_must_be_ordered_in_time() -> None:
    sessions, weekly = _calendar()
    proposed = propose_split(weekly)

    with pytest.raises(SplitError, match="strictly ordered"):
        build_split_plan(
            weekly_observations=weekly,
            calendar=sessions,
            **dict(proposed, test_start=proposed["train_start"]),
        )


def test_shuffled_input_yields_the_same_segments() -> None:
    """Row order must not be able to influence a time-ordered split."""
    sessions, weekly = _calendar()
    proposed = propose_split(weekly)
    ordered = build_split_plan(weekly_observations=weekly, calendar=sessions, **proposed)
    shuffled_source = list(reversed(weekly))

    shuffled = build_split_plan(
        weekly_observations=sorted(shuffled_source), calendar=sessions, **proposed
    )

    assert shuffled.as_payload() == ordered.as_payload()


def test_an_empty_segment_is_an_error_not_an_empty_run() -> None:
    sessions, weekly = _calendar()
    proposed = propose_split(weekly)
    gap_start = proposed["valid_start"] + timedelta(days=1)

    with pytest.raises(SplitError, match="contains no weekly cross-section"):
        build_split_plan(
            weekly_observations=weekly,
            calendar=sessions,
            **dict(proposed, valid_start=gap_start, valid_end=gap_start),
        )
