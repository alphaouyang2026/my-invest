"""LightGBM behind a seam that can be interrupted and inspected.

Two things Qlib's `LGBModel` does not allow, and one thing LightGBM's file format
does not mean.

**A cancel callback cannot be passed in.** `LGBModel.fit()` builds its callbacks
list internally and then expands `**kwargs` after it, so `fit(callbacks=[...])`
raises `TypeError: got multiple values for keyword argument 'callbacks'`. The
subclass here merges instead.

**Callback order is not list order.** `lgb.train` gives any callback without an
explicit `order` a *negative* one (`i - len(callbacks)`) and then sorts, so the
callback appended last runs *first*. Builtin orders are `log_evaluation` 10,
`record_evaluation` 20, `early_stopping` 30. Landing at 25 means the round's
metrics are already recorded when cancellation is observed, and cancellation is
seen before early stopping can end training — with `order > 30` early stopping
raises first and a cancelled run reports success.

**A file checksum is not a model identity.** LightGBM serialises the training
parameters into `model.txt`, including `num_threads`, so the same trees under a
different thread count produce a different sha256. `semantic_checksum` hashes
the file with the runtime-only parameters removed, which is the number to
compare when asking whether two files are the same model.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable

from qlib.contrib.model.gbdt import LGBModel

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Between `record_evaluation` (20) and `early_stopping` (30). See module docstring.
CANCEL_CALLBACK_ORDER = 25

#: How often the callback asks whether cancellation was requested. Every round
#: would be a database round trip per boosting iteration; the design accepts up
#: to this much observation latency and states cancellation as best-effort.
CANCEL_CHECK_EVERY = 20

#: Parameters that describe the machine rather than the model. Everything else
#: in the `parameters:` block — learning rate, the four seeds, regularisation —
#: is research meaning and stays in the semantic checksum.
RUNTIME_ONLY_PARAMETERS = frozenset(
    {
        "num_threads",
        "num_machines",
        "local_listen_port",
        "time_out",
        "machine_list_filename",
        "machines",
        "gpu_platform_id",
        "gpu_device_id",
        "gpu_device_id_list",
        "gpu_use_dp",
        "num_gpu",
        "histogram_pool_size",
        "device_type",
    }
)

_PARAMETER_LINE = re.compile(r"^\[(?P<key>[a-z_]+):")


class TrainingCancelled(RuntimeError):
    """Cancellation was observed during boosting."""


class CancellableLGBModel(LGBModel):
    """`LGBModel` plus one observer. Training itself is unchanged.

    That "unchanged" is load-bearing and is asserted rather than assumed: with
    no cancellation requested, this class must produce a byte-identical
    `model.txt` to the base class on the same data and parameters.
    """

    def __init__(self, *args, should_cancel: Callable[[], bool] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._should_cancel = should_cancel

    def fit(self, dataset, **kwargs):
        if self._should_cancel is None:
            return super().fit(dataset, **kwargs)
        if "callbacks" in kwargs:
            # The very collision this class exists to avoid; a caller reaching
            # past it has misunderstood the seam.
            raise TypeError("callbacks are owned by CancellableLGBModel; pass should_cancel instead")
        return super().fit(dataset, callbacks=[self._cancel_callback()], **kwargs)

    def _cancel_callback(self):
        should_cancel = self._should_cancel

        def callback(env) -> None:
            iteration = env.iteration - env.begin_iteration
            if iteration % CANCEL_CHECK_EVERY:
                return
            if should_cancel():
                logger.info("lightgbm.cancel_observed", iteration=env.iteration)
                raise TrainingCancelled(f"Cancellation observed at boosting round {env.iteration}")

        # Set on the function object, which is where lgb.train looks. Without
        # these two lines the callback is assigned a negative order and runs
        # before the round's metrics are recorded.
        callback.order = CANCEL_CALLBACK_ORDER
        callback.before_iteration = False
        return callback


def model_checksums(model_text: str) -> tuple[str, str]:
    """`(file_checksum, semantic_checksum)` for a serialised booster.

    The first proves the bytes on disk are the bytes that were written. The
    second answers "is this the same model", and is equal across thread counts
    because the runtime parameter lines are dropped before hashing.
    """
    file_checksum = hashlib.sha256(model_text.encode("utf-8")).hexdigest()
    kept = [line for line in model_text.splitlines() if not _is_runtime_parameter(line)]
    semantic = hashlib.sha256("\n".join(kept).encode("utf-8")).hexdigest()
    return file_checksum, semantic


def _is_runtime_parameter(line: str) -> bool:
    match = _PARAMETER_LINE.match(line.strip())
    return bool(match) and match.group("key") in RUNTIME_ONLY_PARAMETERS


def is_constant_model(booster, valid_predictions) -> tuple[bool, list[str]]:
    """Whether the trained model says the same thing about every security.

    `best_iteration == 0` does not catch this. LightGBM emits a single-leaf tree
    when no split is admissible, giving `best_iteration = 1`, one tree, and one
    prediction value for the whole cross-section — and the parameter whitelist
    permits `min_data_in_leaf` up to 5000, which reaches that state easily on
    this ticket's data volume.

    So the test is on the output. Zero variance over the validation segment is
    the criterion closest to what breaks downstream: every `rank_percentile`
    ties and IC is undefined. The other two are reported alongside because they
    point at different causes — no signal, parameters too strict, stopping too
    early.
    """
    reasons: list[str] = []
    if valid_predictions is not None and len(valid_predictions) > 0:
        if float(valid_predictions.std()) == 0.0:
            reasons.append("validation predictions have zero variance")
    if sum(booster.feature_importance("gain")) == 0:
        reasons.append("no feature contributed any gain")
    if booster.num_trees() and all(
        tree["num_leaves"] == 1 for tree in booster.dump_model()["tree_info"]
    ):
        reasons.append("every tree has a single leaf")
    return bool(reasons), reasons
