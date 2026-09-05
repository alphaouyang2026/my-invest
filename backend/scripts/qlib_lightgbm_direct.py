"""Command-line adapter for the synchronous direct Qlib experiment."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from app.db.session import get_sessionmaker

DEFAULT_PROVIDER_ROOT = Path("var/qlib-direct-providers")


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _range(value: str) -> DateRange:
    from app.experiments.qlib_lightgbm_direct import DateRange, DirectExperimentError

    try:
        start, end = value.split(":", maxsplit=1)
        return DateRange(date.fromisoformat(start), date.fromisoformat(end))
    except (ValueError, DirectExperimentError) as exc:
        raise argparse.ArgumentTypeError(f"Expected YYYY-MM-DD:YYYY-MM-DD ({exc})") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build, delete, or consume a filesystem-only Qlib day provider."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("build-provider", "delete-provider"):
        command = subparsers.add_parser(name)
        command.add_argument("--snapshot-id", type=_uuid, required=True)
        command.add_argument("--provider-root", type=Path, default=DEFAULT_PROVIDER_ROOT)

    predict = subparsers.add_parser("predict")
    predict.add_argument("--snapshot-id", type=_uuid, required=True)
    predict.add_argument("--provider-root", type=Path, default=DEFAULT_PROVIDER_ROOT)
    predict.add_argument("--feature-set", choices=("alpha158", "alpha360"), default="alpha158")
    predict.add_argument("--train", type=_range, required=True)
    predict.add_argument("--valid", type=_range, required=True)
    predict.add_argument("--test", type=_range, required=True)
    predict.add_argument(
        "--label-horizon",
        type=int,
        default=5,
        help="Number of trading sessions in the forward-return label (default: 5).",
    )
    predict.add_argument(
        "--rolling-step",
        type=int,
        default=20,
        help="Trading sessions predicted before retraining LightGBM (default: 20).",
    )
    predict.add_argument("--seed", type=int, default=20260829)
    predict.add_argument("--num-threads", type=int, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from app.experiments.qlib_lightgbm_direct import (
        DirectExperimentError,
        DirectPredictionConfig,
        build_day_provider,
        delete_day_provider,
        run_direct_prediction,
    )
    from app.research.day_provider import DayProviderError, provider_path

    try:
        with get_sessionmaker()() as session:
            if args.command == "build-provider":
                result = build_day_provider(session, args.snapshot_id, args.provider_root)
                payload = {
                    "provider_path": str(result.provider.path),
                    "created": result.created,
                    "manifest": result.provider.manifest.to_dict(),
                }
            elif args.command == "delete-provider":
                deleted = delete_day_provider(args.snapshot_id, args.provider_root)
                payload = {
                    "provider_path": str(provider_path(args.snapshot_id, args.provider_root)),
                    "deleted": deleted,
                }
            else:
                result = run_direct_prediction(
                    session,
                    DirectPredictionConfig(
                        snapshot_id=args.snapshot_id,
                        feature_set=args.feature_set,
                        train=args.train,
                        valid=args.valid,
                        test=args.test,
                        provider_root=args.provider_root,
                        seed=args.seed,
                        num_threads=args.num_threads,
                        label_horizon=args.label_horizon,
                        rolling_step=args.rolling_step,
                    ),
                )
                payload = {
                    "summary": result.summary,
                    "daily_ic": _records(result.daily_ic),
                    "predictions": _records(result.predictions),
                    "feature_importance": _records(result.feature_importance),
                }
    except DirectExperimentError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return exc.exit_code
    except DayProviderError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 1
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            default=_json_default,
            allow_nan=False,
        )
    )
    return 0


def _records(frame: pd.DataFrame) -> list[dict]:
    clean = frame.astype(object).where(pd.notna(frame), None)
    return clean.to_dict(orient="records")


def _json_default(value):
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if pd.isna(value):
        return None
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


if __name__ == "__main__":
    raise SystemExit(main())
