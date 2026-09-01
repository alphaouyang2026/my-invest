"""Executable demonstrations for the two P1 findings in 07-design-doc.md.

Run from the repository root with:

    backend/.venv/Scripts/python.exe -m pytest -q -s -p no:cacheprovider backend/tests/test_07_p1_demonstrations.py

The tests intentionally exercise LightGBM's public ``train`` interface.  They
do not depend on the application's future model-training implementation.
"""

from __future__ import annotations

from collections.abc import Callable

import lightgbm as lgb
import numpy as np
import pytest


class ResearchCancelled(RuntimeError):
    """Stand-in for the application's planned cancellation exception."""


def _datasets() -> tuple[lgb.Dataset, lgb.Dataset, np.ndarray]:
    """Return deterministic train/valid data with two usable features."""

    features = np.arange(80, dtype=np.float64).reshape(40, 2)
    labels = np.linspace(-1.0, 1.0, 40, dtype=np.float64)
    train = lgb.Dataset(features[:20], label=labels[:20], free_raw_data=False)
    valid = lgb.Dataset(
        features[20:],
        label=labels[20:],
        reference=train,
        free_raw_data=False,
    )
    return train, valid, features


def _params(*, min_data_in_leaf: int = 2) -> dict[str, object]:
    return {
        "objective": "mse",
        "metric": "l2",
        "verbosity": -1,
        "min_data_in_leaf": min_data_in_leaf,
        "deterministic": True,
        "force_row_wise": True,
        "seed": 7,
        "num_threads": 1,
    }


def test_lgb_train_regular_usage() -> None:
    """Show the ordinary train/validate/early-stop/predict workflow."""

    # 1. Prepare rows drawn from the same distribution.  Only `train_features`
    # and `train_labels` are used to build trees; validation data is reserved
    # for measuring each boosting round and choosing `best_iteration`.
    rng = np.random.default_rng(20260829)
    features = rng.normal(size=(240, 3))
    labels = (
        2.0 * features[:, 0]
        - 0.7 * features[:, 1]
        + 0.2 * rng.normal(size=240)
    )
    train_features, valid_features = features[:180], features[180:]
    train_labels, valid_labels = labels[:180], labels[180:]

    # 2. Convert NumPy arrays into LightGBM Dataset objects.  `reference=train`
    # lets validation reuse the training dataset's feature bin definitions.
    train = lgb.Dataset(train_features, label=train_labels, free_raw_data=False)
    valid = lgb.Dataset(
        valid_features,
        label=valid_labels,
        reference=train,
        free_raw_data=False,
    )

    # 3. Keep the model parameters separate from training-loop controls such
    # as `num_boost_round` and callbacks.
    params = {
        "objective": "mse",
        "metric": "l2",
        "learning_rate": 0.05,
        "num_leaves": 15,
        "min_data_in_leaf": 5,
        "verbosity": -1,
        "deterministic": True,
        "force_row_wise": True,
        "seed": 7,
        "num_threads": 1,
    }
    evaluation_history: dict[str, dict[str, list[float]]] = {}

    # 4. Train only on `train`.  `valid_sets` asks LightGBM to calculate and
    # record metrics after each round.  The validation metric controls early
    # stopping; validation rows do not become training rows.
    model = lgb.train(
        params,
        train,
        num_boost_round=300,
        valid_sets=[train, valid],
        valid_names=["train", "valid"],
        callbacks=[
            lgb.record_evaluation(evaluation_history),
            lgb.early_stopping(20, verbose=False),
        ],
    )

    # 5. Predict with the selected best iteration instead of blindly using the
    # maximum 300 rounds.
    predictions = model.predict(
        valid_features,
        num_iteration=model.best_iteration,
    )

    # 6. Compare against an independent constant baseline: always predicting
    # the mean training label.  A normal useful model should beat it and should
    # produce different scores for different rows.
    baseline_predictions = np.full_like(valid_labels, train_labels.mean())
    model_mse = float(np.mean((valid_labels - predictions) ** 2))
    baseline_mse = float(np.mean((valid_labels - baseline_predictions) ** 2))

    assert 1 <= model.best_iteration <= 300
    assert len(evaluation_history["valid"]["l2"]) >= model.best_iteration
    assert np.ptp(predictions) > 0.0
    assert model_mse < baseline_mse

    print(
        "regular lgb.train:",
        f"best_iteration={model.best_iteration}",
        f"valid_mse={model_mse:.6f}",
        f"constant_baseline_mse={baseline_mse:.6f}",
    )


def test_best_iteration_zero_does_not_detect_a_constant_model() -> None:
    """A one-leaf constant model reports best_iteration == 1, not zero."""

    train, valid, all_features = _datasets()
    model = lgb.train(
        _params(min_data_in_leaf=5_000),
        train,
        num_boost_round=10,
        valid_sets=[train, valid],
        valid_names=["train", "valid"],
        callbacks=[lgb.early_stopping(3, verbose=False)],
    )

    predictions = model.predict(all_features)
    first_tree = model.dump_model()["tree_info"][0]

    # This is the P1: the proposed `best_iteration == 0` guard does not fire.
    assert model.best_iteration == 1
    assert model.best_iteration != 0

    # Nevertheless, the model is semantically empty for ranking.
    assert model.num_trees() == 1
    assert first_tree["num_leaves"] == 1
    assert np.ptp(predictions) == pytest.approx(0.0)


def _raising_cancel_callback() -> Callable[[lgb.callback.CallbackEnv], None]:
    def cancel(_: lgb.callback.CallbackEnv) -> None:
        raise ResearchCancelled("demonstration cancellation")

    return cancel


def _train_until_cancel(
    cancel_callback: Callable[[lgb.callback.CallbackEnv], None],
) -> dict[str, dict[str, list[float]]]:
    train, valid, _ = _datasets()
    evaluation_history: dict[str, dict[str, list[float]]] = {}

    # The custom callback is deliberately appended last, exactly as proposed
    # in the design document.
    callbacks = [
        lgb.log_evaluation(period=0),       # order = 10
        lgb.record_evaluation(evaluation_history),  # order = 20
        lgb.early_stopping(3, verbose=False),       # order = 30
        cancel_callback,                   # no explicit order
    ]

    with pytest.raises(ResearchCancelled):
        lgb.train(
            _params(),
            train,
            num_boost_round=10,
            valid_sets=[train, valid],
            valid_names=["train", "valid"],
            callbacks=callbacks,
        )

    return evaluation_history


def test_appending_cancel_callback_last_makes_it_run_first() -> None:
    """LightGBM sorts by callback.order instead of preserving list order."""

    implicit_order_cancel = _raising_cancel_callback()
    interrupted_history = _train_until_cancel(implicit_order_cancel)

    # LightGBM assigns a negative default order to a callback without one.
    # It therefore runs before built-ins with orders 10, 20, and 30, despite
    # being the final item in the input list.
    assert implicit_order_cancel.order < 10  # type: ignore[attr-defined]
    assert interrupted_history == {}

    # Control case: order 25 means "after record_evaluation, before early
    # stopping".  The current iteration is recorded while cancellation still
    # takes precedence over an early-stop exception.
    explicit_order_cancel = _raising_cancel_callback()
    explicit_order_cancel.order = 25  # type: ignore[attr-defined]
    recorded_history = _train_until_cancel(explicit_order_cancel)

    assert explicit_order_cancel.order == 25  # type: ignore[attr-defined]
    assert len(recorded_history["train"]["l2"]) == 1
    assert len(recorded_history["valid"]["l2"]) == 1
