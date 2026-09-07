"""Plan a search, then run each trial through the direct entry point.

This module decides *which* runs to make and records what they produced. It does
not compute features, split folds, train, or score: a candidate whose leaderboard
position came from a second implementation of those would be a claim about this
module rather than about the model anyone can later run. `direct_config` is the
entire contribution -- one planned trial becomes one `DirectPredictionConfig` --
and `qlib_lightgbm_direct` does the rest.

No ResearchRun, model publication, provider build or database commit occurs here.
All research artifacts are local to the explicitly selected output directory.
"""
from __future__ import annotations

import importlib.metadata
import time
import uuid
from datetime import date
from pathlib import Path

import pandas as pd

from app.experiments.qlib_lightgbm_direct import (
    DateRange, DirectPredictionConfig, run_direct_prediction,
)
from app.experiments.artifact_cache import (
    digest, exclusive_lock, file_digest, read_json, seal, verified, write_json, write_parquet,
)
from app.experiments.search_plan import fold_plan, trial_plan


def code_identity() -> dict:
    backend = Path(__file__).resolve().parents[2]
    files = sorted([*backend.glob("app/**/*.py"), *backend.glob("scripts/*.py"),
                    backend / "pyproject.toml", backend / "uv.lock"])
    return {str(p.relative_to(backend)): file_digest(p) for p in files if p.is_file()}


def environment_identity() -> dict:
    return {name: importlib.metadata.version(name)
            for name in ("pyqlib", "lightgbm", "pandas", "numpy", "scipy", "pyarrow")}


def plan_experiment(config: dict, output: Path, candidates=None):
    from app.db.session import get_sessionmaker
    from app.experiments.qlib_lightgbm_direct import (
        DateRange, DirectPredictionConfig, load_and_validate_snapshot, resolve_feature_set,
    )
    from app.research.day_provider import open_day_provider
    from app.services.stock_pool import DEFAULT_POLICY
    from dataclasses import asdict
    provider = open_day_provider(uuid.UUID(config["snapshot_id"]), Path(config["provider_root"]))
    plan = fold_plan(provider.manifest.calendar, config)
    # Planning needs the snapshot validated, nothing more. Building a second
    # configuration by hand to ask for it put a second mapping of the plan into
    # the module, and its `label_horizon` disagreed with `direct_config`'s.
    probe = direct_config(config, plan, trial_plan(config, candidates)[0])
    with get_sessionmaker()() as session:
        snapshot = load_and_validate_snapshot(session, probe)
        if (str(snapshot.calendar_publication_id) != provider.manifest.calendar_publication_id
                or snapshot.bar_publish_sequence != provider.manifest.snapshot_bar_publish_sequence):
            raise ValueError("Provider and DataSnapshot identities disagree")
        snapshot_identity = dict(id=str(snapshot.id), version=snapshot.version,
                                 bar_publish_sequence=snapshot.bar_publish_sequence,
                                 calendar=str(snapshot.calendar_publication_id))
    context = dict(snapshot=snapshot_identity, provider=provider.manifest.to_dict(),
                   features={alias: resolve_feature_set(alias).definition_payload
                             for alias in config["feature_sets"]},
                   policy=asdict(DEFAULT_POLICY), code=code_identity(), environment=environment_identity())
    trials = trial_plan(config, candidates)
    # Serialize dates/enums to the same JSON-compatible representation used on disk.
    import json
    context = json.loads(json.dumps(context, default=str))
    identity = digest(dict(config=config, context=context, plan=plan, trials=trials))
    root = output / identity[:20]
    manifest = dict(experiment_id=identity, config=config, context=context,
                    usage="development_only_not_blind_test", trial_count=len(trials))
    with exclusive_lock(root):
        if (root / "manifest.json").exists() and read_json(root / "manifest.json") != manifest:
            raise ValueError("Existing experiment manifest differs")
        write_json(root / "manifest.json", manifest)
        write_json(root / "fold_plan.json", plan)
        write_json(root / "trial_plan.json", trials)
    return root, provider


def summarize_daily(daily: pd.DataFrame, expected_dates: int) -> dict:
    values = daily["rank_ic"].dropna()
    per_fold = daily.groupby("fold")["rank_ic"].mean()
    def ratio(series):
        std = series.std(ddof=1)
        return float(series.mean()/std) if pd.notna(std) and std > 0 else None
    def mean(series):
        return float(series.mean()) if series.notna().any() else None
    return dict(rank_ic_mean=mean(values), ic_mean=mean(daily["ic"]),
                rank_icir=ratio(values), icir=ratio(daily["ic"].dropna()),
                positive_rank_ic_fraction=float((values > 0).mean()) if len(values) else None,
                valid_dates=len(values), expected_dates=expected_dates,
                coverage=len(values)/expected_dates,
                fold_rank_ic={str(k): (float(v) if pd.notna(v) else None) for k,v in per_fold.items()},
                worst_fold=mean(per_fold.nsmallest(1)),
                without_best_fold=mean(daily.loc[daily["fold"] != per_fold.idxmax(), "rank_ic"])
                if len(per_fold)>1 and per_fold.notna().any() else None,
                eligible=len(values)==expected_dates)


def direct_config(manifest_config: dict, plan: dict, trial: dict) -> DirectPredictionConfig:
    """Turn one planned trial into the run the direct entry point will execute.

    This is the whole of what a search contributes to a run: which dataset, which
    segments, which parameters. Everything after this point -- features, folds,
    training, scoring -- is the direct entry point's, so a trial's leaderboard
    position and a later command-line run mean the same thing.

    The first fold's own train and valid bounds are passed, because the direct
    plan slides them by `rolling_step` exactly as `fold_plan` does. `purge_horizon`
    is passed explicitly so that trials at different label horizons keep sharing
    one set of evaluation dates and stay comparable.

    `search_report.direct_run_config` renders the same compilation as the JSON a
    command line consumes. Two compilers would let the run a search measured and
    the run a person repeats drift apart while both looked correct.
    """
    first, last = plan["folds"][0], plan["folds"][-1]
    return DirectPredictionConfig(
        snapshot_id=uuid.UUID(manifest_config["snapshot_id"]),
        feature_set=trial["feature_set"],
        train=DateRange(*map(date.fromisoformat, first["train"])),
        valid=DateRange(*map(date.fromisoformat, first["valid"])),
        test=DateRange(
            date.fromisoformat(first["evaluation"][0]),
            date.fromisoformat(last["evaluation"][1]),
        ),
        provider_root=Path(manifest_config["provider_root"]),
        seed=trial["seed"],
        num_threads=manifest_config["num_threads"],
        label_horizon=trial["horizon"],
        rolling_step=manifest_config["step"],
        model_params=trial["model_params"],
        stop_metric=trial["stop_metric"],
        train_window=trial["train_window"],
        purge_horizon=manifest_config["purge_horizon"],
        # Absent from configurations written before the cache existed; the run's
        # own defaults are the right answer there rather than a hard failure.
        **{
            name: manifest_config[name]
            for name in ("feature_batch_size", "qlib_kernels", "scan_batch_rows")
            if name in manifest_config
        },
    )


def run_trial(target: Path, trial: dict, plan: dict, context: dict) -> dict:
    """Run one trial through the direct entry point and record what it produced."""
    from app.db.session import get_sessionmaker

    started = time.monotonic()
    config = direct_config(context["config"], plan, trial)
    with get_sessionmaker()() as session:
        result = run_direct_prediction(
            session, config, cache_root=context["cache_root"], artifact_dir=target
        )
    daily = result.daily_ic
    write_parquet(target / "daily_metrics.parquet", daily)
    write_parquet(target / "predictions.parquet", result.predictions)
    write_parquet(target / "feature_importance.parquet", result.feature_importance)
    summary = summarize_daily(daily, len(plan["common_dates"]))
    folds = result.summary["folds"]
    summary.update(
        folds=folds,
        elapsed_seconds=time.monotonic() - started,
        best_iteration_one_fraction=sum(f["best_iteration"] == 1 for f in folds) / len(folds),
    )
    write_json(target / "metrics.json", summary)
    return summary


def successful(root: Path, trial: dict):
    folder = root / "trials" / trial["trial_id"]
    pointer = folder / "success.json"
    if not pointer.exists():
        return None
    attempt = (folder / read_json(pointer)["attempt"]).resolve()
    if not attempt.is_relative_to(folder.resolve()):
        raise ValueError("Invalid attempt pointer")
    if not verified(attempt, digest(trial)):
        raise ValueError("Incomplete success artifact")
    return attempt


def _attempt(root, trial, plan, context):
    folder = root / "trials" / trial["trial_id"]
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder / "config.json", trial)
    number = len(list(folder.glob("attempt-*"))) + 1
    target = folder / f"attempt-{number:04d}"
    target.mkdir()
    write_json(target / "config.json", trial)
    print(f"trial {trial['trial_id']} attempt {number}", flush=True)
    try:
        run_trial(target, trial, plan, context)
        seal(target, digest(trial))
        write_json(folder / "success.json", dict(attempt=target.name))
        return True
    except Exception as exc:
        import traceback
        frames = [dict(file=Path(f.filename).name, line=f.lineno, function=f.name)
                  for f in traceback.extract_tb(exc.__traceback__)]
        write_json(target / "failure.json", dict(error_type=type(exc).__name__,
                   phase="train_and_evaluate", frames=frames,
                   hint="Inspect validation/data/configuration; rerun under debugger for details"))
        print(f"FAILED {trial['trial_id']}: {type(exc).__name__}", flush=True)
        return False


def run_search(root: Path, provider, *, resume=False, max_trials=None, workers=1):
    """Run every unfinished trial, one at a time, and report what happened.

    Serial by construction. Each trial now assembles its own segments from the
    shared feature cache, and two of them at once would double peak memory while
    contending for the same cache lock -- the resource ceiling this search exists
    to respect. `workers` is accepted so existing commands keep working.
    """
    from itertools import groupby
    if workers != 1:
        raise ValueError("workers must be 1: each trial loads its own bounded segments")
    manifest = read_json(root / "manifest.json")
    if manifest["context"]["code"] != code_identity() or manifest["context"]["environment"] != environment_identity():
        raise ValueError("Code or environment changed since planning; create a new experiment")
    trials = read_json(root / "trial_plan.json")
    plan = read_json(root / "fold_plan.json")
    # One cache for the whole search, beside the experiments rather than inside
    # one of them, so a later stage over the same data reuses what this computed.
    context = dict(config=manifest["config"], cache_root=root.parent / "_cache")
    completed = failures = attempted = 0
    with exclusive_lock(root):
        pending = []
        for trial in trials:
            if successful(root, trial):
                if not resume:
                    raise ValueError("Completed trials exist; use --resume")
                completed += 1
                continue
            pending.append(trial)
        if max_trials is not None:
            pending = pending[:max_trials]
        # Sorted before grouping, because `groupby` only groups adjacent keys and
        # a plan is not required to emit one dataset's trials consecutively. The
        # order decides only how often the cache is cold, never the results.
        key = lambda trial: (trial["feature_set"], trial["horizon"])
        for _, group in groupby(sorted(pending, key=key), key=key):
            for trial in group:
                succeeded = _attempt(root, trial, plan, context)
                attempted += 1
                completed += int(succeeded)
                failures += int(not succeeded)
    return dict(attempted=attempted, completed=completed, failed=failures, planned=len(trials))
