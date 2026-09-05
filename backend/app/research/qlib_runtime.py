from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pandas as pd

from app.core.logging import get_logger

logger = get_logger(__name__)


def runtime_identity() -> dict:
    import qlib

    lockfile = Path(__file__).resolve().parents[2] / "uv.lock"
    return {
        "pyqlib_version": qlib.__version__,
        "dependency_lock_sha256": hashlib.sha256(lockfile.read_bytes()).hexdigest(),
        "bundle_exporter_schema": "1",
    }


def read_features(
    bundle_path: Path,
    *,
    instruments: list[str] | str,
    fields: list[str],
    start: str,
    end: str,
    kernels: int | None = None,
) -> pd.DataFrame:
    """The only application seam that initializes Qlib global provider state.
    """
    import qlib
    from qlib.config import REG_CN
    from qlib.data import D

    started = time.monotonic()
    options = dict(provider_uri=str(bundle_path), region=REG_CN,
                   expression_cache=None, dataset_cache=None, clear_mem_cache=True)
    if kernels is not None:
        if type(kernels) is not int or kernels < 1:
            raise ValueError("kernels must be a positive integer")
        options["kernels"] = kernels
    qlib.init(**options)
    # Worth a line of its own: this is the one place global Qlib state is set
    # up, and "which bundle was it actually reading" is the first question a
    # surprising result raises.
    logger.info("qlib.initialized", provider_uri=str(bundle_path), version=qlib.__version__)
    requested = D.instruments(market="all") if instruments == "all" else instruments
    frame = D.features(requested, fields, start_time=start, end_time=end, freq="day")
    logger.info(
        "qlib.features_read",
        fields=list(fields),
        start=start,
        end=end,
        rows=len(frame),
        seconds=round(time.monotonic() - started, 3),
    )
    return frame


def analyze_signals(predictions: pd.DataFrame, labels: pd.DataFrame) -> dict:
    """Run Qlib's public signal-analysis recorder behind the worker-only seam."""
    from qlib.workflow.record_temp import SigAnaRecord

    recorder = _MemoryRecorder(
        {
            "pred.pkl": predictions.sort_index(),
            "label.pkl": labels.sort_index(),
        }
    )
    SigAnaRecord(recorder=recorder).generate()
    logger.debug(
        "qlib.signals_analyzed",
        predictions=len(predictions),
        labels=len(labels),
        metrics=sorted(recorder.metrics),
    )
    return {
        "ic": recorder.objects["sig_analysis/ic.pkl"],
        "rank_ic": recorder.objects["sig_analysis/ric.pkl"],
        "metrics": recorder.metrics,
    }


class _MemoryRecorder:
    """Minimal recorder adapter; Qlib's pickle/MLflow layout never escapes this module.

    In memory rather than MLflow-backed, and now necessarily so: mlflow 3.15.2
    refuses to open a filesystem tracking store at all. It is also the better
    answer regardless — the design keeps nothing in a recorder directory, so not
    creating one removes both the deletion step and the temptation to read it.
    """

    def __init__(self, objects: dict[str, object] | None = None) -> None:
        self.objects = objects if objects is not None else {}
        self.metrics: dict[str, float] = {}
        #: `SignalRecord.generate` logs it; nothing reads it back.
        self.experiment_id = "in-memory"
        self.id = "in-memory"

    def load_object(self, path: str):
        from qlib.utils.exceptions import LoadObjectError

        try:
            return self.objects[path]
        except KeyError as exc:
            raise LoadObjectError(path) from exc

    def list_artifacts(self, artifact_path: str = "") -> list[str]:
        prefix = f"{artifact_path}/" if artifact_path else ""
        return [path for path in self.objects if path.startswith(prefix)]

    def save_objects(self, *, artifact_path: str | None = None, **objects) -> None:
        prefix = f"{artifact_path}/" if artifact_path else ""
        self.objects.update({f"{prefix}{name}": value for name, value in objects.items()})

    def log_metrics(self, **metrics) -> None:
        self.metrics.update(metrics)


def run_signal_analysis(model, dataset) -> dict:
    """Qlib's own signal records, over an in-memory recorder.

    `SigAnaRecord` reads `pred.pkl` and `label.pkl`, which `SignalRecord`
    produces. Running the second without the first does not raise — the base
    class catches the missing dependency, logs "The dependent data does not
    exists. Generation skipped." and returns None. So the return value is
    checked: treating "no exception" as "analysis ran" would report an empty
    result as a successful one.

    `SignalRecord` covers the test segment only, which is what it is for here —
    the product metrics for all three segments come from the local statistics
    module so that the model and the momentum control share one code path. This
    exists to prove the Qlib stack is really wired up, and its IC is asserted
    against the local figure in the integration test.

    The label it saves is `DK_R`, the raw forward return rather than the
    rank-normalised training target, so the IC below is computed against actual
    returns.
    """
    from qlib.workflow.record_temp import SigAnaRecord, SignalRecord

    recorder = _MemoryRecorder()
    SignalRecord(model=model, dataset=dataset, recorder=recorder).generate()
    if "pred.pkl" not in recorder.objects:
        raise RuntimeError("SignalRecord produced no predictions")

    produced = SigAnaRecord(recorder=recorder).generate()
    if produced is None:
        raise RuntimeError(
            "SigAnaRecord skipped: its dependent records were missing, so no analysis was run"
        )
    logger.info("qlib.signal_analysis_generated", metrics=sorted(recorder.metrics))
    return {
        "predictions": recorder.objects["pred.pkl"],
        "labels": recorder.objects.get("label.pkl"),
        "ic": recorder.objects.get("sig_analysis/ic.pkl"),
        "rank_ic": recorder.objects.get("sig_analysis/ric.pkl"),
        "metrics": dict(recorder.metrics),
    }
