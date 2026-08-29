"""What a model experiment *is*, reduced to one fingerprint.

Ticket 06 established that identical research meaning must reuse one
`ResearchExperiment` and any change of meaning must create a new one. For a model
experiment the meaning is wider than 06's two window parameters, and most of the
additions are things a name alone cannot pin down:

- the feature set, expanded column by column, not `("alpha158_jp_v1", 1)`;
- the processor pipeline, with a semantics version on anything we wrote
  ourselves, because a class name plus kwargs cannot notice that its own
  implementation changed;
- the fit window, so the "processors only fit on train" contract is part of the
  identity even while no fitted processor exists to consume it;
- the resolved LightGBM parameters, including the four expanded seeds.

`num_threads` is excluded on purpose. With `deterministic` and `force_row_wise`
set, LightGBM returns the same trees at any thread count, so folding the thread
count into identity would turn a resource setting into a new experiment.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from app.research.feature_sets import FeatureSet
from app.research.splits import SplitPlan

WEEKLY_LABEL_DEFINITION = "next_rebalance_open_to_next_rebalance_open"
MIN_COVERAGE = 0.90
MIN_VALID_SECURITIES = 100
GROUP_COUNT = 5

#: Fixed by the design, and not reachable through the override whitelist.
#: `objective` is the research target; the other two are what make the run
#: reproducible, and a caller able to switch them off could quietly void the
#: reproducibility guarantee the ticket is accepted against.
LOCKED_PARAMS: dict[str, Any] = {
    "objective": "mse",
    "deterministic": True,
    "force_row_wise": True,
}

#: Baseline `lgbm_jp_baseline_v1`. Far smaller than Qlib's published Alpha158
#: parameters, which were tuned on CSI300 over a decade; on ~85 weekly
#: cross-sections `num_leaves=210` would memorise the training range.
DEFAULT_PARAMS: dict[str, Any] = {
    "learning_rate": 0.05,
    "num_leaves": 31,
    "max_depth": 6,
    "min_data_in_leaf": 200,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l1": 1.0,
    "lambda_l2": 10.0,
    "num_boost_round": 1000,
    "early_stopping_rounds": 50,
}

#: name -> (type, low, high, low_inclusive, high_inclusive)
OVERRIDABLE_PARAMS: dict[str, tuple[type, float, float, bool, bool]] = {
    "learning_rate": (float, 0.0, 0.5, False, True),
    "num_leaves": (int, 2, 255, True, True),
    "max_depth": (int, 2, 12, True, True),
    "min_data_in_leaf": (int, 20, 5000, True, True),
    "feature_fraction": (float, 0.0, 1.0, False, True),
    "bagging_fraction": (float, 0.0, 1.0, False, True),
    "bagging_freq": (int, 0, 10, True, True),
    "lambda_l1": (float, 0.0, 1000.0, True, True),
    "lambda_l2": (float, 0.0, 1000.0, True, True),
    "num_boost_round": (int, 1, 5000, True, True),
    "early_stopping_rounds": (int, 1, 500, True, True),
}

#: One seed in, four seeds out. They are refused inside `model_params` so the
#: request cannot carry two disagreeing sources of the same fact — a precedence
#: rule would only ever be read by someone already confused about which value ran.
SEED_KEYS = ("seed", "bagging_seed", "feature_fraction_seed", "data_random_seed")
DEFAULT_SEED = 20260829

DEFAULT_FEATURE_SET = "alpha158_jp_v1"


class ParameterError(ValueError):
    """A rejected override. Surfaces as a 400."""


def resolve_model_params(overrides: dict[str, Any] | None, *, seed: int) -> dict[str, Any]:
    """Merge defaults, overrides and locked values into what LightGBM will run.

    Rejects rather than clamps. A clamped value leaves the caller believing they
    ran the parameters they typed while the fingerprint records different ones.
    """
    overrides = dict(overrides or {})

    for key in SEED_KEYS:
        if key in overrides:
            raise ParameterError(
                f"{key!r} must be given as the top-level 'seed' field, not inside model_params"
            )
    unknown = sorted(set(overrides) - set(OVERRIDABLE_PARAMS))
    if unknown:
        locked = sorted(set(unknown) & set(LOCKED_PARAMS))
        if locked:
            raise ParameterError(f"Parameter(s) {locked} are fixed by the design and cannot be overridden")
        raise ParameterError(
            f"Unknown model parameter(s) {unknown}; allowed: {sorted(OVERRIDABLE_PARAMS)}"
        )

    resolved = dict(DEFAULT_PARAMS)
    for key, value in overrides.items():
        expected, low, high, low_ok, high_ok = OVERRIDABLE_PARAMS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParameterError(f"{key!r} must be a number, got {type(value).__name__}")
        if expected is int and not float(value).is_integer():
            raise ParameterError(f"{key!r} must be an integer, got {value!r}")
        value = expected(value)
        if (value < low if low_ok else value <= low) or (value > high if high_ok else value >= high):
            bounds = f"{'[' if low_ok else '('}{low}, {high}{']' if high_ok else ')'}"
            raise ParameterError(f"{key!r} must lie in {bounds}, got {value!r}")
        resolved[key] = value

    if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed < 2**31:
        raise ParameterError(f"seed must be a 32-bit non-negative integer, got {seed!r}")
    resolved.update({key: seed for key in SEED_KEYS})
    resolved.update(LOCKED_PARAMS)
    return dict(sorted(resolved.items()))


def processor_payload() -> dict[str, list[dict[str, Any]]]:
    """The pipeline, exactly as it enters the fingerprint and the manifest.

    `InfToNaN` carries a `semantics_version` because it is ours: editing its
    `__call__` would change what a run means while leaving class name and kwargs
    untouched. Qlib's own processors carry none — their behaviour is pinned by
    `pyqlib==0.9.7`, and a pyqlib upgrade is handled as a design review rather
    than by a field here.
    """
    from app.research.processors import INF_TO_NAN_SEMANTICS_VERSION

    return {
        "infer": [
            {
                "class": "InfToNaN",
                "semantics_version": INF_TO_NAN_SEMANTICS_VERSION,
                "kwargs": {"fields_group": "feature"},
            }
        ],
        "learn": [
            {"class": "DropnaLabel", "kwargs": {}},
            {"class": "CSRankNorm", "kwargs": {"fields_group": "label"}},
        ],
    }


@dataclass(frozen=True)
class ModelResearchDefinition:
    """The immutable, canonical meaning of one model experiment."""

    data_snapshot_id: UUID
    feature_set: FeatureSet
    split: SplitPlan
    stock_pool_policy_fingerprint: str
    model_params: dict[str, Any]
    seed: int = DEFAULT_SEED
    processors: dict[str, list[dict[str, Any]]] = field(default_factory=processor_payload)

    @property
    def fit_window(self) -> dict[str, str]:
        """Declared even though no fitted processor consumes it today.

        Leaving it unset would let the first fitted processor someone adds
        estimate its parameters over the whole range without any error — the
        exact leak the contract exists to prevent.
        """
        return {
            "fit_start_time": self.split.train.start.isoformat(),
            "fit_end_time": self.split.train.end.isoformat(),
        }

    @property
    def required_history_days(self) -> int:
        # The momentum control needs 147 sessions whatever the feature set asks
        # for, because it is computed on the same cross-sections for comparison.
        return max(self.feature_set.max_window, 147)

    @property
    def canonical_payload(self) -> dict[str, Any]:
        return {
            "kind": "model",
            "data_snapshot_id": str(self.data_snapshot_id),
            "feature_set": self.feature_set.definition_payload,
            "label": {"weekly": WEEKLY_LABEL_DEFINITION},
            "splits": self.split.as_payload(),
            "model_params": self.model_params,
            "processors": self.processors,
            "fit_window": self.fit_window,
            "stock_pool_policy_fingerprint": self.stock_pool_policy_fingerprint,
            "evaluation": {
                "factor_coverage": MIN_COVERAGE,
                "label_coverage": MIN_COVERAGE,
                "min_valid_securities": MIN_VALID_SECURITIES,
                "group_count": GROUP_COUNT,
                "ic": "pearson",
                "rank_ic": "spearman_average_rank",
                "icir": "mean_over_sample_std_unannualized",
            },
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.canonical_payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
