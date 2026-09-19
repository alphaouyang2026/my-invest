"""Executable demonstrations for the two P1 findings in 07-design-doc.md.

Run from the repository root with:

    backend/.venv/Scripts/python.exe -m pytest -q -s -p no:cacheprovider backend/tests/test_07_p1_demonstrations.py

The last two tests demonstrate the ``feval`` mechanism that
``app.experiments.booster_training`` relies on, over twelve hand-written rows.

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


def _tiny_rows() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Twelve hand-written rows of ``y = 2 * x1 + x2``, small enough to read.

    No noise: every metric below can be recomputed by hand from these numbers.
    """

    train_features = np.array(
        [
            [1.0, 0.0],
            [2.0, 1.0],
            [3.0, 0.0],
            [4.0, 2.0],
            [5.0, 1.0],
            [6.0, 3.0],
            [7.0, 2.0],
            [8.0, 4.0],
        ]
    )
    train_labels = np.array([2.0, 5.0, 6.0, 10.0, 11.0, 15.0, 16.0, 20.0])
    valid_features = np.array([[1.5, 0.5], [3.5, 1.5], [5.5, 2.5], [7.5, 3.5]])
    valid_labels = np.array([3.5, 8.5, 13.5, 18.5])
    return train_features, train_labels, valid_features, valid_labels


def _tiny_params() -> dict[str, object]:
    """Overfit-fast settings; `metric="None"` leaves only what `feval` reports."""

    return {
        "objective": "regression",
        "learning_rate": 0.5,
        "num_leaves": 4,
        "min_data_in_leaf": 1,  # eight rows would otherwise refuse to split
        "min_data_in_bin": 1,
        "metric": "None",
        "verbosity": -1,
        "deterministic": True,
        "force_row_wise": True,
        "seed": 0,
        "num_threads": 1,
    }


def test_feval_reports_a_custom_metric() -> None:
    """A `feval` returning one tuple replaces the built-in metric entirely."""

    train_features, train_labels, valid_features, valid_labels = _tiny_rows()

    # 1. The evaluation function receives the current predictions and the
    # Dataset being scored.  Labels come back out of the Dataset; anything
    # else the metric needs must be captured from the enclosing scope.
    def mean_absolute_error(
        predictions: np.ndarray,
        eval_data: lgb.Dataset,
    ) -> tuple[str, float, bool]:
        labels = eval_data.get_label()
        # (name, value, higher_is_better) -- False means early stopping should
        # look for a decreasing value.
        return "my_mae", float(np.mean(np.abs(predictions - labels))), False

    train = lgb.Dataset(train_features, label=train_labels, feature_name=["x1", "x2"])
    valid = lgb.Dataset(valid_features, label=valid_labels, reference=train)
    evaluation_history: dict[str, dict[str, list[float]]] = {}

    # 2. `metric="None"` in the params is what keeps the built-in `l2` out of
    # the history; without it `l2` would also be recorded, ahead of `my_mae`.
    model = lgb.train(
        _tiny_params(),
        train,
        num_boost_round=5,
        valid_sets=[valid],
        valid_names=["valid"],
        feval=mean_absolute_error,
        callbacks=[lgb.record_evaluation(evaluation_history)],
    )

    # 3. The recorded series is exactly what the function returned, keyed by
    # the name it chose.
    recorded = evaluation_history["valid"]["my_mae"]
    assert list(evaluation_history) == ["valid"]
    assert list(evaluation_history["valid"]) == ["my_mae"]
    assert len(recorded) == 5

    # 4. Recompute the final round independently to show nothing is hidden
    # between `feval` and the history.
    predictions = model.predict(valid_features)
    assert recorded[-1] == pytest.approx(float(np.mean(np.abs(predictions - valid_labels))))

    # 5. Eight rows at learning_rate=0.5 overfit within a handful of rounds:
    # the validation metric bottoms out in the middle, not at the last round.
    assert np.argmin(recorded) == 2
    assert recorded[2] < recorded[0]
    assert recorded[-1] > recorded[2]

    print("feval single metric:", [round(value, 4) for value in recorded])


def test_feval_metric_order_selects_the_early_stopping_target() -> None:
    """With `first_metric_only`, the returned list's order chooses the target.

    This is the mechanism behind `booster_training.evaluate`: both metrics are
    always computed and recorded, and only their order decides which one stops
    training.
    """

    train_features, train_labels, valid_features, valid_labels = _tiny_rows()

    # 1. One evaluation function reporting two metrics with opposite
    # directions.  `stop_metric` moves the chosen one to the front.
    def make_feval(
        stop_metric: str,
    ) -> Callable[[np.ndarray, lgb.Dataset], list[tuple[str, float, bool]]]:
        def evaluate(
            predictions: np.ndarray,
            eval_data: lgb.Dataset,
        ) -> list[tuple[str, float, bool]]:
            labels = eval_data.get_label()
            metrics = [
                ("corr", float(np.corrcoef(predictions, labels)[0, 1]), True),
                ("my_mae", float(np.mean(np.abs(predictions - labels))), False),
            ]
            return metrics if stop_metric == "corr" else metrics[::-1]

        return evaluate

    def run(stop_metric: str) -> tuple[int, dict[str, list[float]]]:
        train = lgb.Dataset(train_features, label=train_labels, feature_name=["x1", "x2"])
        valid = lgb.Dataset(valid_features, label=valid_labels, reference=train)
        history: dict[str, dict[str, list[float]]] = {}
        model = lgb.train(
            _tiny_params(),
            train,
            num_boost_round=20,
            valid_sets=[valid],
            valid_names=["valid"],
            feval=make_feval(stop_metric),
            callbacks=[
                lgb.record_evaluation(history),
                lgb.early_stopping(3, first_metric_only=True, verbose=False),
            ],
        )
        return model.best_iteration, history["valid"]

    # 2. Stopping on `my_mae` ends at the round that minimises it.
    mae_iteration, mae_history = run("my_mae")
    assert mae_iteration == 3
    assert np.argmin(mae_history["my_mae"]) == mae_iteration - 1
    assert len(mae_history["my_mae"]) == mae_iteration + 3  # patience rounds

    # 3. Stopping on `corr` keeps going: correlation creeps up in the fourth
    # decimal for the whole budget, so no round is ever three-rounds stale.
    corr_iteration, corr_history = run("corr")
    assert len(corr_history["corr"]) == 20
    assert corr_iteration > mae_iteration

    # 4. Both metrics are computed either way -- only the choice of round
    # differs.  The shared prefix of the two runs is identical, which is what
    # makes `stop_metric` a selection knob rather than a change of training.
    shared = len(mae_history["my_mae"])
    assert mae_history["my_mae"] == corr_history["my_mae"][:shared]
    assert mae_history["corr"] == corr_history["corr"][:shared]

    # 5. Stopping late on `corr` costs real accuracy on `my_mae`.
    assert corr_history["my_mae"][corr_iteration - 1] > mae_history["my_mae"][mae_iteration - 1]

    print(
        "feval metric order:",
        f"stop_on_my_mae -> best_iteration={mae_iteration} in {shared} rounds",
        f"stop_on_corr -> best_iteration={corr_iteration} in 20 rounds",
    )
