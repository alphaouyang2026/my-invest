"""Command-line adapter for the synchronous direct Qlib experiment."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from app.db.session import get_sessionmaker

DEFAULT_PROVIDER_ROOT = Path("var/qlib-direct-providers")
#: Mirrors app.experiments.qlib_lightgbm_direct.EXPANDING, restated so argparse
#: never has to import that module. The round-trip test pins them together.
EXPANDING = "expanding"

#: The only default this command adds. The configuration deliberately requires a
#: feature set -- there is no neutral answer for a library caller -- but a bare
#: `predict` is more useful running the baseline than printing an error.
COMMAND_DEFAULTS = {"feature_set": "alpha158"}


def predict_defaults() -> dict:
    """The run configuration's own defaults, not a second copy of them.

    Restating them here would let this command drift from what it configures, and
    the drift would look like a deliberate command-line default.
    """
    from app.experiments.qlib_lightgbm_direct import DirectPredictionConfig

    derived = {
        field.name: field.default
        for field in dataclasses.fields(DirectPredictionConfig)
        if field.default is not dataclasses.MISSING
    }
    return derived | COMMAND_DEFAULTS


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _range(value: str) -> tuple[date, date]:
    """Parse without importing the experiment module.

    argparse runs this during `parse_args`, so importing Qlib here would happen
    before `main` can point its import-time chatter away from stdout.
    """
    try:
        start, end = (date.fromisoformat(part) for part in value.split(":", maxsplit=1))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Expected YYYY-MM-DD:YYYY-MM-DD ({exc})") from exc
    if start > end:
        raise argparse.ArgumentTypeError(f"Date range starts after it ends: {start}..{end}")
    return start, end


#: PowerShell writes a BOM for its own `utf8`, and a file argument is exactly the
#: route a Windows caller takes when the shell mangles inline JSON. Reading files
#: as utf-8-sig accepts both; plain UTF-8 is unaffected.
FILE_ENCODING = "utf-8-sig"


def _json_object(value: str) -> dict:
    """Inline JSON, or `@path` to read it from a file the search wrote."""
    try:
        text = (
            Path(value[1:]).read_text(encoding=FILE_ENCODING)
            if value.startswith("@")
            else value
        )
        loaded = json.loads(text)
    except (OSError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"Expected a JSON object or @path ({exc})") from exc
    if not isinstance(loaded, dict):
        raise argparse.ArgumentTypeError(f"Expected a JSON object, got {type(loaded).__name__}")
    return loaded


def _train_window(value: str) -> int | str:
    if value == EXPANDING:
        return EXPANDING
    try:
        window = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Expected {EXPANDING!r} or an integer ({exc})") from exc
    if window < 1:
        raise argparse.ArgumentTypeError(f"train_window must be positive, got {window}")
    return window


#: How a `--config` value becomes what DirectPredictionConfig expects.
PREDICT_PARSERS = {
    "snapshot_id": _uuid,
    "provider_root": Path,
    "train": _range,
    "valid": _range,
    "test": _range,
    "train_window": _train_window,
}
def predict_fields() -> tuple[str, ...]:
    """Every field `predict` accepts, in the order the configuration declares them."""
    from app.experiments.qlib_lightgbm_direct import DirectPredictionConfig

    return tuple(field.name for field in dataclasses.fields(DirectPredictionConfig))


REQUIRED_PREDICT_FIELDS = ("snapshot_id", "train", "valid", "test")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build, delete, or consume a filesystem-only Qlib day provider."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("build-provider", "delete-provider"):
        command = subparsers.add_parser(name)
        command.add_argument("--snapshot-id", type=_uuid, required=True)
        command.add_argument("--provider-root", type=Path, default=DEFAULT_PROVIDER_ROOT)

    # Every predict default is None so an explicit flag stays distinguishable from
    # an omitted one, and `--config` can supply the rest. Defaults live in DEFAULTS.
    predict = subparsers.add_parser("predict")
    predict.add_argument(
        "--config",
        type=Path,
        help="JSON written by evaluate_qlib_candidates.py; explicit flags override it.",
    )
    predict.add_argument("--snapshot-id", type=_uuid)
    predict.add_argument("--provider-root", type=Path)
    predict.add_argument("--feature-set", choices=("alpha158", "alpha360"))
    predict.add_argument("--train", type=_range)
    predict.add_argument("--valid", type=_range)
    predict.add_argument("--test", type=_range)
    predict.add_argument(
        "--label-horizon",
        type=int,
        help="Number of trading sessions in the forward-return label (default: 5).",
    )
    predict.add_argument(
        "--rolling-step",
        type=int,
        help="Trading sessions predicted before retraining LightGBM (default: 20).",
    )
    predict.add_argument("--seed", type=int)
    predict.add_argument("--num-threads", type=int)
    predict.add_argument(
        "--model-params",
        type=_json_object,
        help="Resolved LightGBM overrides as JSON or @path; the whitelist still applies.",
    )
    predict.add_argument(
        "--stop-metric",
        choices=("l2", "rank_ic"),
        help="Validation metric that selects the boosting round (default: l2).",
    )
    predict.add_argument(
        "--purge-horizon",
        type=int,
        help="Label lookahead the segment seams are purged by (default: the label horizon).",
    )
    predict.add_argument(
        "--train-window",
        type=_train_window,
        help="'expanding', or a fixed number of trading days that slides with the fold.",
    )
    return parser



def _predict_config(parser, args):
    """Merge `--config`, explicit flags and defaults into one DirectPredictionConfig.

    A flag always wins over the file, so a candidate export can be re-run with one
    field deliberately changed without editing the export.
    """
    from app.experiments.qlib_lightgbm_direct import DateRange, DirectPredictionConfig

    fields = predict_fields()
    values = predict_defaults()
    if args.config is not None:
        try:
            loaded = json.loads(args.config.read_text(encoding=FILE_ENCODING))
        except (OSError, ValueError) as exc:
            parser.error(f"--config is not readable JSON: {exc}")
        if not isinstance(loaded, dict):
            parser.error("--config must contain a JSON object")
        unknown = sorted(set(loaded) - set(fields))
        if unknown:
            parser.error(f"--config has unknown field(s) {unknown}; allowed: {list(fields)}")
        for field, value in loaded.items():
            convert = PREDICT_PARSERS.get(field)
            try:
                values[field] = convert(value) if convert else value
            except argparse.ArgumentTypeError as exc:
                parser.error(f"--config field {field!r}: {exc}")
    for field in fields:
        given = getattr(args, field, None)
        if given is not None:
            values[field] = given
    missing = [field for field in REQUIRED_PREDICT_FIELDS if values.get(field) is None]
    if missing:
        parser.error(f"Missing required field(s): {', '.join('--' + f.replace('_', '-') for f in missing)}")
    for field in ("train", "valid", "test"):
        values[field] = DateRange(*values[field])
    return DirectPredictionConfig(**values)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # This command's stdout carries exactly one JSON document, so that
    # `predict > result.json` is parseable. Several things below write to stdout
    # uninvited -- Qlib announces its absent optional backends at import, and
    # structlog's out-of-the-box console renderer prints every log line there --
    # and listing them individually would only hold until the next one appears.
    # So the whole body runs with stdout pointed at stderr, and the payload is
    # written to the real stdout captured here. Diagnostics stay visible on the
    # terminal; only the result is redirectable.
    stdout = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        return _run(parser, args, stdout)


def _run(parser, args, stdout) -> int:
    from app.experiments.qlib_lightgbm_direct import (
        DirectExperimentError,
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
                result = run_direct_prediction(session, _predict_config(parser, args))
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
        ),
        file=stdout,
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
