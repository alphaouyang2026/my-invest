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
) -> pd.DataFrame:
    """The only application seam that initializes Qlib global provider state.
    """
    import qlib
    from qlib.config import REG_CN
    from qlib.data import D

    started = time.monotonic()
    qlib.init(
        provider_uri=str(bundle_path),
        region=REG_CN,
        expression_cache=None,
        dataset_cache=None,
        clear_mem_cache=True,
    )
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
    """Minimal recorder adapter; Qlib's pickle/MLflow layout never escapes this module."""

    def __init__(self, objects: dict[str, object]) -> None:
        self.objects = objects
        self.metrics: dict[str, float] = {}

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
