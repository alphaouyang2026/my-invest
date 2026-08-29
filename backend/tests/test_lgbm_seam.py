"""The LightGBM seam: cancellation ordering, model identity, constant models.

These run against the real LightGBM rather than a fake. Every property under
test is a property of the library — callback ordering, what goes into
`model.txt`, when a single-leaf tree is emitted — and a fake would only assert
what this file already assumes.

No bundle and no network, so they belong in the ordinary CI tier. Determinism in
particular has to live there: it is the property most easily broken by an
unthinking parameter edit, and it must fail in the fastest loop, not only in the
container suite.
"""

from __future__ import annotations

import lightgbm as lgb
import numpy as np
import pytest

from app.research.lgbm import (
    CANCEL_CALLBACK_ORDER,
    CANCEL_CHECK_EVERY,
    TrainingCancelled,
    is_constant_model,
    model_checksums,
)
from app.research.model_definition import resolve_model_params


#: Large enough that the baseline `min_data_in_leaf=200` can actually split.
#: On 300 training rows it cannot, and LightGBM answers with a single-leaf tree
#: — the same degenerate model `is_constant_model` exists to reject. Worth
#: noting for section 13.1: a toy golden fixture would train nothing at all.
TRAIN_ROWS, VALID_ROWS = 4000, 1000


def _dataset(rows: int = TRAIN_ROWS + VALID_ROWS, features: int = 6, seed: int = 0):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(rows, features))
    y = x[:, 0] * 1.5 + rng.normal(scale=0.3, size=rows)
    return x, y


def _train(*, params_override=None, num_threads: int = 1, callbacks=None, rounds: int = 30):
    x, y = _dataset()
    params = resolve_model_params(params_override, seed=11)
    params = {k: v for k, v in params.items() if k not in {"num_boost_round", "early_stopping_rounds"}}
    params["num_threads"] = num_threads
    params["verbosity"] = -1
    return lgb.train(
        params,
        lgb.Dataset(x[:TRAIN_ROWS], label=y[:TRAIN_ROWS]),
        num_boost_round=rounds,
        valid_sets=[lgb.Dataset(x[TRAIN_ROWS:], label=y[TRAIN_ROWS:])],
        valid_names=["valid"],
        callbacks=callbacks or [],
    )


# --------------------------------------------------------------------------
# Callback ordering
# --------------------------------------------------------------------------


def test_a_callback_without_an_order_runs_before_the_builtins() -> None:
    """Why `order = 25` has to be set explicitly.

    `lgb.train` assigns `i - len(callbacks)` to any callback that does not
    declare an order, so appending one last makes it run *first* — before the
    round's metrics are recorded.
    """
    seen: list[int] = []

    def plain(env) -> None:
        seen.append(env.iteration)

    recorded: dict = {}
    _train(callbacks=[lgb.record_evaluation(recorded), plain], rounds=3)

    assert plain.order < 0
    assert plain.order < lgb.record_evaluation(recorded).order


def test_the_cancel_callback_sits_between_record_evaluation_and_early_stopping() -> None:
    assert lgb.log_evaluation(period=0).order < CANCEL_CALLBACK_ORDER
    assert lgb.record_evaluation({}).order < CANCEL_CALLBACK_ORDER
    assert CANCEL_CALLBACK_ORDER < lgb.early_stopping(5, verbose=False).order


def test_cancellation_wins_when_it_lands_on_an_early_stopping_round() -> None:
    """The failure `order > 30` would produce.

    Early stopping raises its own exception to end training. Ordered after it,
    the cancel callback never runs and the user who pressed cancel is told the
    run succeeded.
    """
    recorded: dict = {}

    def cancel(env) -> None:
        raise TrainingCancelled(f"cancelled at {env.iteration}")

    cancel.order = CANCEL_CALLBACK_ORDER
    cancel.before_iteration = False

    with pytest.raises(TrainingCancelled):
        _train(
            callbacks=[
                lgb.record_evaluation(recorded),
                lgb.early_stopping(1, verbose=False),
                cancel,
            ],
            rounds=50,
        )
    # The round's metrics were recorded before cancellation was observed, which
    # is what ordering after `record_evaluation` (20) buys.
    assert recorded["valid"]["l2"]


def test_the_check_interval_bounds_observation_latency() -> None:
    """Cancellation is best-effort, and this is the size of "best".

    The callback reads the flag every `CANCEL_CHECK_EVERY` rounds rather than
    every round, so a request can wait that long to be seen — and if training
    ends first, the run is allowed to succeed.
    """
    checks: list[int] = []

    def cancel(env) -> None:
        if (env.iteration - env.begin_iteration) % CANCEL_CHECK_EVERY == 0:
            checks.append(env.iteration)

    cancel.order = CANCEL_CALLBACK_ORDER
    cancel.before_iteration = False
    _train(callbacks=[cancel], rounds=45)

    assert checks == [0, CANCEL_CHECK_EVERY, 2 * CANCEL_CHECK_EVERY]


# --------------------------------------------------------------------------
# Model identity
# --------------------------------------------------------------------------


def test_thread_count_changes_the_file_checksum_but_not_the_model() -> None:
    """Why `TrainedModel` carries two checksums.

    `deterministic` plus `force_row_wise` give identical trees at any thread
    count, but LightGBM writes `[num_threads: N]` into `model.txt`, so the file
    hash differs. Comparing file hashes across thread counts would report a
    model change that did not happen.
    """
    one = _train(num_threads=1)
    four = _train(num_threads=4)

    assert np.array_equal(one.predict(_dataset()[0]), four.predict(_dataset()[0]))
    file_one, semantic_one = model_checksums(one.model_to_string())
    file_four, semantic_four = model_checksums(four.model_to_string())

    assert file_one != file_four
    assert semantic_one == semantic_four


def test_the_same_thread_count_reproduces_the_file_byte_for_byte() -> None:
    assert _train(num_threads=1).model_to_string() == _train(num_threads=1).model_to_string()


def test_the_semantic_checksum_still_notices_a_research_parameter() -> None:
    """The runtime filter must not be so broad that it hides real changes."""
    base = model_checksums(_train().model_to_string())[1]
    changed = model_checksums(_train(params_override={"learning_rate": 0.2}).model_to_string())[1]

    assert base != changed


# --------------------------------------------------------------------------
# Constant models
# --------------------------------------------------------------------------


def test_a_single_leaf_model_is_caught_even_though_best_iteration_is_not_zero() -> None:
    """The gap the old `best_iteration == 0` rule left open.

    With `min_data_in_leaf` above the training row count no split is admissible, so
    LightGBM emits one single-leaf tree: `best_iteration` is 1, and every
    security receives the same score.
    """
    booster = _train(params_override={"min_data_in_leaf": TRAIN_ROWS + 1}, rounds=5)
    predictions = booster.predict(_dataset()[0][TRAIN_ROWS:])

    assert booster.num_trees() >= 1
    assert float(predictions.std()) == 0.0

    constant, reasons = is_constant_model(booster, predictions)

    assert constant
    assert "validation predictions have zero variance" in reasons


def test_a_model_that_learned_something_is_not_flagged() -> None:
    booster = _train(rounds=30)
    predictions = booster.predict(_dataset()[0][TRAIN_ROWS:])

    constant, reasons = is_constant_model(booster, predictions)

    assert not constant
    assert reasons == []
