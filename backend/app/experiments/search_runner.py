"""Read-only snapshot research: shared data preparation, training, and resumable trials.

No ResearchRun, model publication, provider build or database commit occurs here.
All research artifacts are local to the explicitly selected output directory.
"""
from __future__ import annotations

import importlib.metadata
import os
import time
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from app.experiments.search_plan import (
    digest, exclusive_lock, file_digest, fold_plan, read_json, seal, trial_plan, verified, write_json,
)


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
        DateRange, DirectPredictionConfig, _load_and_validate_snapshot, _resolve_feature_set,
    )
    from app.research.day_provider import open_day_provider
    from app.services.stock_pool import DEFAULT_POLICY
    from dataclasses import asdict
    provider = open_day_provider(uuid.UUID(config["snapshot_id"]), Path(config["provider_root"]))
    plan = fold_plan(provider.manifest.calendar, config)
    f = plan["folds"][0]
    direct = DirectPredictionConfig(
        snapshot_id=uuid.UUID(config["snapshot_id"]), feature_set=config["feature_sets"][0],
        train=DateRange(*map(date.fromisoformat, f["train"])),
        valid=DateRange(*map(date.fromisoformat, f["valid"])),
        test=DateRange(*map(date.fromisoformat, f["evaluation"])),
        provider_root=Path(config["provider_root"]), seed=config["seed"],
        num_threads=config["num_threads"], label_horizon=config["purge_horizon"],
    )
    with get_sessionmaker()() as session:
        snapshot = _load_and_validate_snapshot(session, direct)
        if (str(snapshot.calendar_publication_id) != provider.manifest.calendar_publication_id
                or snapshot.bar_publish_sequence != provider.manifest.snapshot_bar_publish_sequence):
            raise ValueError("Provider and DataSnapshot identities disagree")
        snapshot_identity = dict(id=str(snapshot.id), version=snapshot.version,
                                 bar_publish_sequence=snapshot.bar_publish_sequence,
                                 calendar=str(snapshot.calendar_publication_id))
    context = dict(snapshot=snapshot_identity, provider=provider.manifest.to_dict(),
                   features={alias: _resolve_feature_set(alias).definition_payload
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


def parquet(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    frame.to_parquet(temp)
    os.replace(temp, path)


@dataclass(frozen=True)
class MatrixSegment:
    """One bounded model segment; feature storage is allocated exactly once."""

    features: np.ndarray
    labels: np.ndarray
    index: pd.MultiIndex
    feature_names: tuple[str, ...]


@dataclass(frozen=True)
class ShardedData:
    """Lightweight reference to verified feature shards and narrow label data."""

    root: Path
    feature_names: tuple[str, ...]
    horizon: int
    scan_batch_rows: int
    sizes: dict

    def load_segment(self, bounds, *, learning: bool) -> MatrixSegment:
        import pyarrow.dataset as ds

        start, end = map(pd.Timestamp, bounds)
        label_column = f"learn_h{self.horizon}" if learning else f"raw_h{self.horizon}"
        labels = pd.read_parquet(
            self.root / "labels.parquet",
            columns=["datetime", "instrument", label_column],
            filters=[("datetime", ">=", start), ("datetime", "<=", end)],
        )
        labels["datetime"] = pd.to_datetime(labels["datetime"])
        labels["instrument"] = labels["instrument"].astype(str)
        if labels.duplicated(["datetime", "instrument"]).any():
            raise ValueError("Duplicate label row")
        all_index = pd.MultiIndex.from_frame(labels[["datetime", "instrument"]])
        if learning:
            labels = labels.loc[labels[label_column].notna()].copy()
        labels = labels.sort_values(["datetime", "instrument"], kind="stable")
        target = pd.MultiIndex.from_frame(labels[["datetime", "instrument"]])
        values = np.empty((len(target), len(self.feature_names)), dtype=np.float32)
        written = np.zeros(len(target), dtype=bool)
        paths = sorted(self.root.glob("batch-*/features.parquet"))
        if not paths:
            raise ValueError("No feature shards")
        dataset = ds.dataset([str(p) for p in paths], format="parquet")
        scanner = dataset.scanner(
            columns=["datetime", "instrument", *self.feature_names],
            filter=(ds.field("datetime") >= start.to_datetime64()) &
                   (ds.field("datetime") <= end.to_datetime64()),
            batch_size=self.scan_batch_rows, batch_readahead=1, fragment_readahead=1,
            use_threads=False,
        )
        for batch in scanner.to_batches():
            frame = batch.to_pandas()
            keys = pd.MultiIndex.from_arrays(
                [pd.to_datetime(frame["datetime"]), frame["instrument"].astype(str)],
                names=target.names,
            )
            positions = target.get_indexer(keys)
            unknown = positions < 0
            if unknown.any() and (all_index.get_indexer(keys[unknown]) < 0).any():
                raise ValueError("Unknown feature row")
            selected = positions >= 0  # DropnaLabel excludes other valid feature rows.
            selected_positions = positions[selected]
            if len(np.unique(selected_positions)) != len(selected_positions) or written[selected_positions].any():
                raise ValueError("Duplicate feature row")
            values[selected_positions] = frame.loc[selected, list(self.feature_names)].to_numpy(
                dtype=np.float32, copy=False)
            written[selected_positions] = True
        if not written.all():
            raise ValueError(f"Missing feature rows: {int((~written).sum())}")
        return MatrixSegment(values, labels[label_column].to_numpy(dtype=np.float32),
                             target, self.feature_names)


def prepare_data(root: Path, provider, feature: str, horizon: int):
    """Build verified, bounded feature shards and return a lightweight reference."""
    manifest = read_json(root / "manifest.json")
    plan = read_json(root / "fold_plan.json")
    data_id = digest(dict(context=manifest.get("context", {}), plan=plan,
                          feature_sets=manifest["config"]["feature_sets"],
                          cache_schema=manifest["config"]["cache_schema_version"]))
    cache = root.parent / "_cache" / data_id[:20]
    with exclusive_lock(cache):
        return _prepare_data(root, provider, feature, horizon, cache, data_id)


def _prepare_data(root: Path, provider, feature: str, horizon: int, cache: Path, data_id: str):
    from app.db.session import get_sessionmaker
    from app.experiments.qlib_lightgbm_direct import (
        DateRange, DirectPredictionConfig, _build_universe, _load_and_validate_snapshot,
        _resolve_feature_set, _restrict_to_universe,
    )
    from app.research.qlib_runtime import read_features
    from app.services.calendar_port import SessionCalendarPort
    manifest = read_json(root / "manifest.json")
    config = manifest["config"]
    plan = read_json(root / "fold_plan.json")
    first, last = plan["folds"][0], plan["folds"][-1]
    direct = DirectPredictionConfig(
        snapshot_id=uuid.UUID(config["snapshot_id"]), feature_set=feature,
        train=DateRange(*map(date.fromisoformat, first["train"])),
        valid=DateRange(*map(date.fromisoformat, first["valid"])),
        test=DateRange(date.fromisoformat(first["evaluation"][0]),
                       date.fromisoformat(last["evaluation"][1])),
        provider_root=Path(config["provider_root"]), label_horizon=horizon,
    )
    universe_dir = cache / "universe"
    universe_id = digest([data_id, "universe"])
    if not verified(universe_dir, universe_id):
        max_window = max(_resolve_feature_set(a).max_window for a in config["feature_sets"])
        dates = [date.fromisoformat(d) for d in plan["calendar"]
                 if config["train_start"] <= d <= last["evaluation"][1]]
        with get_sessionmaker()() as session:
            snapshot = _load_and_validate_snapshot(session, direct)
            cal = SessionCalendarPort(session, snapshot.calendar_publication_id)
            universe, sizes, _, warnings, fingerprint = _build_universe(
                session, snapshot, cal, dict(train=dates, valid=(), test=()), max_window)
        write_json(universe_dir / "data.json", dict(
            universe={str(d): sorted(v) for d, v in universe.items()},
            sizes={str(d): v for d, v in sizes.items()}, warnings=warnings, policy=fingerprint))
        seal(universe_dir, universe_id)
    saved = read_json(universe_dir / "data.json")
    universe = {date.fromisoformat(d): set(v) for d, v in saved["universe"].items()}
    sizes = {date.fromisoformat(d): n for d, n in saved["sizes"].items()}
    spec = _resolve_feature_set(feature)
    feature_root = cache / feature
    feature_id = digest([data_id, feature, spec.definition_checksum, config["horizons"],
                         config["feature_batch_size"], config["qlib_kernels"]])
    if not verified(feature_root, feature_id):
        securities = sorted(set().union(*universe.values()))
        batches = [securities[i:i + config["feature_batch_size"]]
                   for i in range(0, len(securities), config["feature_batch_size"])]
        shard_plan = dict(identity=feature_id, schema_version=config["cache_schema_version"],
                          securities=securities, batch_size=config["feature_batch_size"],
                          batch_count=len(batches), batches=[dict(
                              batch_id=f"batch-{i:04d}", securities=members,
                              securities_digest=digest(members)) for i, members in enumerate(batches, 1)])
        plan_path = feature_root / "shard-plan.json"
        if plan_path.exists() and read_json(plan_path) != shard_plan:
            raise ValueError("Feature shard plan changed")
        write_json(plan_path, shard_plan)
        fields = list(dict.fromkeys([*spec.expressions, "$close"]))
        for batch_spec in shard_plan["batches"]:
            batch_root = feature_root / batch_spec["batch_id"]
            batch_id = digest([feature_id, batch_spec])
            if verified(batch_root, batch_id):
                continue
            raw = read_features(provider.path, instruments=batch_spec["securities"], fields=fields,
                                start=provider.manifest.coverage_start.isoformat(),
                                end=provider.manifest.coverage_end.isoformat(),
                                kernels=config["qlib_kernels"])
            if raw.empty:
                raise ValueError(f"Empty feature shard: {batch_spec['batch_id']}")
            restricted = _restrict_to_universe(raw, universe)
            features = restricted[list(spec.expressions)].replace([np.inf, -np.inf], np.nan).copy()
            features.columns = list(spec.column_names)
            features = features.swaplevel().sort_index()
            features.index = features.index.set_names(["datetime", "instrument"])
            feature_frame = features.reset_index()
            feature_frame["instrument"] = feature_frame["instrument"].astype(str)
            parquet(batch_root / "features.parquet", feature_frame)
            close = raw["$close"]
            grouped = close.groupby(level="instrument")
            label_frame = feature_frame[["datetime", "instrument"]].copy()
            feature_index = features.index
            for label_horizon in config["horizons"]:
                forward = grouped.shift(-(label_horizon + 1)) / grouped.shift(-1) - 1
                forward = forward.swaplevel().sort_index().reindex(feature_index)
                label_frame[f"raw_h{label_horizon}"] = forward.to_numpy()
            parquet(batch_root / "raw-labels.parquet", label_frame)
            write_json(batch_root / "metadata.json", dict(
                batch_id=batch_spec["batch_id"], securities=batch_spec["securities"],
                securities_digest=batch_spec["securities_digest"], rows=len(feature_frame),
                feature_columns=list(spec.column_names)))
            del raw, restricted, features, feature_frame, label_frame
            seal(batch_root, batch_id)
        _validate_shard_set(feature_root, shard_plan, feature_id)
        raw_labels = [pd.read_parquet(feature_root / b["batch_id"] / "raw-labels.parquet")
                      for b in shard_plan["batches"]]
        labels = pd.concat(raw_labels, ignore_index=True)
        del raw_labels
        if labels.duplicated(["datetime", "instrument"]).any():
            raise ValueError("Duplicate labels across feature shards")
        for label_horizon in config["horizons"]:
            source = f"raw_h{label_horizon}"
            labels[f"learn_h{label_horizon}"] = (
                labels.groupby("datetime", group_keys=False)[source].rank(pct=True) - .5
            ) * 3.46
        labels = labels.sort_values(["datetime", "instrument"], kind="stable")
        parquet(feature_root / "labels.parquet", labels)
        write_json(feature_root / "dataset-metadata.json", dict(
            shard_plan_digest=file_digest(plan_path), batch_ids=[b["batch_id"] for b in shard_plan["batches"]],
            horizons=config["horizons"], rows=len(labels)))
        seal(feature_root, feature_id)
    shard_plan = read_json(feature_root / "shard-plan.json")
    _validate_shard_set(feature_root, shard_plan, feature_id)
    if not verified(cache / "universe", digest([data_id, "universe"])):
        raise ValueError("Missing universe cache")
    return ShardedData(feature_root, tuple(spec.column_names), horizon,
                       config["scan_batch_rows"], sizes)


def _validate_shard_set(root: Path, plan: dict, identity: str) -> None:
    expected = [b["batch_id"] for b in plan["batches"]]
    actual = sorted(p.name for p in root.glob("batch-*") if p.is_dir())
    if actual != expected or len(expected) != plan["batch_count"]:
        raise ValueError("Feature shard set is incomplete or contains unexpected batches")
    members = [security for batch in plan["batches"] for security in batch["securities"]]
    if len(members) != len(set(members)) or sorted(members) != plan["securities"]:
        raise ValueError("Feature shard securities are incomplete or duplicated")
    for batch in plan["batches"]:
        if digest(batch["securities"]) != batch["securities_digest"]:
            raise ValueError("Feature shard securities digest mismatch")
        batch_root = root / batch["batch_id"]
        if not verified(batch_root, digest([identity, batch])):
            raise ValueError("Incomplete feature shard")
        metadata = read_json(batch_root / "metadata.json")
        if metadata["securities"] != batch["securities"]:
            raise ValueError("Feature shard metadata differs from plan")
        feature_path = batch_root / "features.parquet"
        label_path = batch_root / "raw-labels.parquet"
        columns = ["datetime", "instrument"]
        feature_keys = pd.read_parquet(feature_path, columns=columns)
        label_keys = pd.read_parquet(label_path, columns=columns)
        if len(feature_keys) != metadata["rows"] or len(label_keys) != metadata["rows"]:
            raise ValueError("Feature shard row count differs from metadata")
        if feature_keys.duplicated(columns).any() or label_keys.duplicated(columns).any():
            raise ValueError("Duplicate index inside feature shard")
        if not feature_keys.equals(label_keys):
            raise ValueError("Feature and label shard indexes differ")


def slice_dates(frame, bounds):
    dates = frame.index.get_level_values("datetime")
    return frame.loc[(dates >= pd.Timestamp(bounds[0])) & (dates <= pd.Timestamp(bounds[1]))]


def daily_rank_ic(predictions, labels, dates, min_count=100) -> float:
    from scipy.stats import rankdata
    p, y = np.asarray(predictions), np.asarray(labels)
    values = []
    for day in pd.unique(dates):
        keep = (dates == day) & np.isfinite(p) & np.isfinite(y)
        if keep.sum() < min_count:
            continue
        pr, yr = rankdata(p[keep]), rankdata(y[keep])
        if np.std(pr) > 0 and np.std(yr) > 0:
            values.append(float(np.corrcoef(pr, yr)[0, 1]))
    if not values:
        raise ValueError("No valid daily Rank IC: constant predictions or insufficient securities")
    return float(np.mean(values))


def fit_model(train: pd.DataFrame | MatrixSegment, valid: pd.DataFrame | MatrixSegment,
              trial: dict, num_threads: int):
    """No test data is accepted at this seam. First metric alone selects the round."""
    import lightgbm as lgb
    from app.experiments.qlib_lightgbm_direct import _feature_label_parts
    if isinstance(train, MatrixSegment):
        tx, ty, train_dates = train.features, train.labels, train.index.get_level_values("datetime")
        vx, vy, valid_dates = valid.features, valid.labels, valid.index.get_level_values("datetime")
        feature_names = list(train.feature_names)
        empty = not len(tx) or not len(vx)
    else:
        tx, ty = _feature_label_parts(train)
        vx, vy = _feature_label_parts(valid)
        train_dates, valid_dates = tx.index.get_level_values("datetime"), vx.index.get_level_values("datetime")
        feature_names = list(tx.columns)
        empty = train.empty or valid.empty
    ty_values, vy_values = np.asarray(ty), np.asarray(vy)
    if empty or not np.isfinite(ty_values).all() or not np.isfinite(vy_values).all():
        raise ValueError("Empty segment or nonfinite learning labels")
    params = dict(trial["resolved_params"])
    rounds = params.pop("num_boost_round")
    patience = params.pop("early_stopping_rounds")
    params.update(metric="None", verbosity=-1, num_threads=num_threads)
    ts = lgb.Dataset(tx, label=ty_values, feature_name=feature_names)
    vs = lgb.Dataset(vx, label=vy, reference=ts)
    rank_evaluators = {id(ts): rank_evaluator(ty_values, train_dates),
                       id(vs): rank_evaluator(vy_values, valid_dates)}

    def evaluate(pred, data):
        y = data.get_label()
        rank = rank_evaluators[id(data)](pred)
        metrics = [("rank_ic", rank, True), ("l2", float(np.mean((pred-y)**2)), False)]
        return metrics if trial["stop_metric"] == "rank_ic" else metrics[::-1]

    history = {}
    booster = lgb.train(params, ts, num_boost_round=rounds, valid_sets=[ts, vs],
                        valid_names=["train", "valid"], feval=evaluate,
                        callbacks=[lgb.record_evaluation(history),
                                   lgb.early_stopping(patience, first_metric_only=True, verbose=False)])
    curve = pd.DataFrame([dict(dataset=ds, metric=metric, iteration=i+1, value=float(v))
                          for ds, metrics in history.items() for metric, values in metrics.items()
                          for i, v in enumerate(values)])
    info = dict(best_iteration=int(booster.best_iteration),
                evaluated_rounds=len(history["valid"]["l2"]),
                constant_baseline_l2=float(np.mean((vy_values-float(ty_values.mean()))**2)),
                best_valid_l2=float(history["valid"]["l2"][booster.best_iteration-1]),
                best_valid_rank_ic=float(history["valid"]["rank_ic"][booster.best_iteration-1]))
    return booster, curve, info


def rank_evaluator(labels, dates, min_count=100):
    """Precompute label ranks; avoid rescanning the entire panel once per date per round."""
    codes, _ = pd.factorize(dates, sort=True)
    counts = np.bincount(codes)
    yr = pd.Series(labels).groupby(codes).rank(method="average").to_numpy()
    yc = yr - (np.bincount(codes, weights=yr)/counts)[codes]
    yy = np.bincount(codes, weights=yc*yc)

    def evaluate(pred):
        if not np.isfinite(pred).all():
            raise ValueError("Nonfinite training predictions")
        ranks = pd.Series(pred).groupby(codes).rank(method="average").to_numpy()
        centered = ranks - (np.bincount(codes, weights=ranks)/counts)[codes]
        xx = np.bincount(codes, weights=centered*centered)
        xy = np.bincount(codes, weights=centered*yc)
        valid = (counts >= min_count) & (xx > 0) & (yy > 0)
        if not valid.any():
            raise ValueError("No valid daily Rank IC")
        return float(np.mean(xy[valid]/np.sqrt(xx[valid]*yy[valid])))
    return evaluate


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


def _run_legacy_trial(target: Path, trial: dict, plan: dict, data, num_threads: int) -> dict:
    from app.research.model_evaluation import evaluate_segment, feature_importance_frame, score_frame
    from app.research.lgbm import is_constant_model
    learn, infer, labels, sizes = data
    curves, daily_parts, prediction_parts, importance_parts, fold_info = [], [], [], [], []
    started = time.monotonic()
    for f in plan["folds"]:
        bounds = list(f["train"])
        if trial["train_window"] != "expanding":
            end = plan["calendar"].index(bounds[1])
            bounds[0] = plan["calendar"][end-trial["train_window"]+1]
        train, valid = slice_dates(learn, bounds), slice_dates(learn, f["valid"])
        # DropnaLabel may not silently remove an entire validation date.
        expected = [d for d in plan["calendar"] if f["valid"][0] <= d <= f["valid"][1]]
        counts = valid.groupby(level="datetime").size()
        for d in expected:
            n = int(counts.get(pd.Timestamp(d), 0))
            if n < 100 or n / sizes[date.fromisoformat(d)] < .9:
                raise ValueError(f"Fold {f['fold']} validation label coverage fails on {d}")
        booster, curve, info = fit_model(train, valid, trial, num_threads)
        test = slice_dates(infer, f["evaluation"])["feature"]
        predictions = pd.Series(booster.predict(test, num_iteration=booster.best_iteration),
                                index=test.index, name="score")
        constant, reasons = is_constant_model(booster, predictions)
        if constant or not np.isfinite(predictions).all():
            raise ValueError(f"Degenerate predictions in fold {f['fold']}: {reasons}")
        daily = evaluate_segment(score_frame(predictions), labels, universe_sizes=sizes,
                                 segment="development", source="lightgbm")
        daily.insert(0, "fold", f["fold"])
        pred = predictions.to_frame().join(slice_dates(infer, f["evaluation"])["label"].rename(columns={"LABEL0":"label"}))
        pred.insert(0, "fold", f["fold"])
        importance = feature_importance_frame(booster, list(test.columns))
        importance.insert(0, "fold", f["fold"])
        curve.insert(0, "fold", f["fold"])
        model_path = target / "models" / f"fold-{f['fold']}.txt"
        model_path.parent.mkdir(parents=True, exist_ok=True)
        booster.save_model(str(model_path), num_iteration=booster.best_iteration)
        fold_info.append(info | dict(fold=f["fold"], train=bounds, valid=f["valid"],
                                    evaluation=f["evaluation"], train_rows=len(train), valid_rows=len(valid),
                                    test_rows=len(test), unique_scores=int(predictions.nunique()),
                                    score_std=float(predictions.std()),
                                    score_unique_fraction=float(predictions.nunique()/len(predictions))))
        curves.append(curve); daily_parts.append(daily); prediction_parts.append(pred); importance_parts.append(importance)
    all_daily = pd.concat(daily_parts, ignore_index=True)
    parquet(target / "learning_curves.parquet", pd.concat(curves, ignore_index=True))
    parquet(target / "daily_metrics.parquet", all_daily)
    parquet(target / "predictions.parquet", pd.concat(prediction_parts))
    parquet(target / "feature_importance.parquet", pd.concat(importance_parts, ignore_index=True))
    summary = summarize_daily(all_daily, len(plan["common_dates"]))
    summary.update(folds=fold_info, elapsed_seconds=time.monotonic()-started,
                   best_iteration_one_fraction=sum(f["best_iteration"]==1 for f in fold_info)/len(fold_info))
    write_json(target / "metrics.json", summary)
    return summary


def run_trial(target: Path, trial: dict, plan: dict, data, num_threads: int) -> dict:
    if not isinstance(data, ShardedData):
        return _run_legacy_trial(target, trial, plan, data, num_threads)
    from app.research.model_evaluation import evaluate_segment, feature_importance_frame, score_frame
    from app.research.lgbm import is_constant_model

    curves, daily_parts, prediction_parts, importance_parts, fold_info = [], [], [], [], []
    started = time.monotonic()
    for f in plan["folds"]:
        bounds = list(f["train"])
        if trial["train_window"] != "expanding":
            end = plan["calendar"].index(bounds[1])
            bounds[0] = plan["calendar"][end-trial["train_window"]+1]
        train = data.load_segment(bounds, learning=True)
        valid = data.load_segment(f["valid"], learning=True)
        expected = [d for d in plan["calendar"] if f["valid"][0] <= d <= f["valid"][1]]
        counts = pd.Series(1, index=valid.index).groupby(level="datetime").size()
        for day in expected:
            n = int(counts.get(pd.Timestamp(day), 0))
            if n < 100 or n / data.sizes[date.fromisoformat(day)] < .9:
                raise ValueError(f"Fold {f['fold']} validation label coverage fails on {day}")
        train_rows, valid_rows = len(train.index), len(valid.index)
        booster, curve, info = fit_model(train, valid, trial, num_threads)
        del train, valid

        test = data.load_segment(f["evaluation"], learning=False)
        predictions = pd.Series(
            booster.predict(test.features, num_iteration=booster.best_iteration),
            index=test.index, name="score",
        )
        constant, reasons = is_constant_model(booster, predictions)
        if constant or not np.isfinite(predictions).all():
            raise ValueError(f"Degenerate predictions in fold {f['fold']}: {reasons}")
        raw_labels = pd.DataFrame({
            "observation_date": test.index.get_level_values("datetime").date,
            "instrument_id": test.index.get_level_values("instrument").astype(str),
            "label": test.labels,
            "label_reason": np.where(np.isfinite(test.labels), None, "label_unavailable"),
        })
        daily = evaluate_segment(score_frame(predictions), raw_labels, universe_sizes=data.sizes,
                                 segment="development", source="lightgbm")
        daily.insert(0, "fold", f["fold"])
        pred = predictions.to_frame()
        pred["label"] = test.labels
        pred.insert(0, "fold", f["fold"])
        importance = feature_importance_frame(booster, list(test.feature_names))
        importance.insert(0, "fold", f["fold"])
        curve.insert(0, "fold", f["fold"])
        model_path = target / "models" / f"fold-{f['fold']}.txt"
        model_path.parent.mkdir(parents=True, exist_ok=True)
        booster.save_model(str(model_path), num_iteration=booster.best_iteration)
        fold_info.append(info | dict(
            fold=f["fold"], train=bounds, valid=f["valid"], evaluation=f["evaluation"],
            train_rows=train_rows, valid_rows=valid_rows, test_rows=len(test.index),
            unique_scores=int(predictions.nunique()), score_std=float(predictions.std()),
            score_unique_fraction=float(predictions.nunique()/len(predictions))))
        del test
        curves.append(curve); daily_parts.append(daily); prediction_parts.append(pred); importance_parts.append(importance)
    all_daily = pd.concat(daily_parts, ignore_index=True)
    parquet(target / "learning_curves.parquet", pd.concat(curves, ignore_index=True))
    parquet(target / "daily_metrics.parquet", all_daily)
    parquet(target / "predictions.parquet", pd.concat(prediction_parts))
    parquet(target / "feature_importance.parquet", pd.concat(importance_parts, ignore_index=True))
    summary = summarize_daily(all_daily, len(plan["common_dates"]))
    summary.update(folds=fold_info, elapsed_seconds=time.monotonic()-started,
                   best_iteration_one_fraction=sum(f["best_iteration"]==1 for f in fold_info)/len(fold_info))
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


def _attempt(root, trial, plan, data, num_threads, preparation_error=None):
    folder = root / "trials" / trial["trial_id"]
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder / "config.json", trial)
    number = len(list(folder.glob("attempt-*"))) + 1
    target = folder / f"attempt-{number:04d}"
    target.mkdir()
    write_json(target / "config.json", trial)
    print(f"trial {trial['trial_id']} attempt {number}", flush=True)
    phase = "prepare_data" if preparation_error is not None else "train_and_evaluate"
    try:
        if preparation_error is not None:
            raise preparation_error
        run_trial(target, trial, plan, data, num_threads)
        seal(target, digest(trial))
        write_json(folder / "success.json", dict(attempt=target.name))
        return True
    except Exception as exc:
        import traceback
        frames = [dict(file=Path(f.filename).name, line=f.lineno, function=f.name)
                  for f in traceback.extract_tb(exc.__traceback__)]
        write_json(target / "failure.json", dict(error_type=type(exc).__name__, phase=phase, frames=frames,
                   hint="Inspect validation/data/configuration; rerun under debugger for details"))
        print(f"FAILED {trial['trial_id']}: {type(exc).__name__}", flush=True)
        return False


def run_search(root: Path, provider, *, resume=False, max_trials=None, workers=1):
    from concurrent.futures import ThreadPoolExecutor
    from itertools import groupby
    if workers not in (1, 2):
        raise ValueError("workers must be 1 or 2")
    manifest = read_json(root / "manifest.json")
    if manifest["config"].get("cache_schema_version", 1) >= 2 and workers != 1:
        raise ValueError("Sharded data preparation requires workers=1 until resource validation passes")
    if manifest["context"]["code"] != code_identity() or manifest["context"]["environment"] != environment_identity():
        raise ValueError("Code or environment changed since planning; create a new experiment")
    trials = read_json(root / "trial_plan.json")
    plan = read_json(root / "fold_plan.json")
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
        # Qlib initialization and preparation remain serial. Threads share one read-only
        # prepared panel; each booster/dataset/attempt is independent. Never clone panels
        # into unbounded worker processes.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for key, group in groupby(pending, key=lambda t:(t["feature_set"],t["horizon"])):
                data, error = None, None
                try:
                    data = prepare_data(root, provider, *key)
                except Exception as exc:
                    error = exc
                group = list(group)
                if error is not None:
                    results = [_attempt(root,t,plan,None,manifest["config"]["num_threads"],error) for t in group]
                else:
                    results = []
                    for offset in range(0,len(group),workers):
                        futures = [pool.submit(_attempt, root,t,plan,data,manifest["config"]["num_threads"])
                                   for t in group[offset:offset+workers]]
                        results.extend(future.result() for future in futures)
                attempted += len(results)
                completed += sum(results)
                failures += len(results)-sum(results)
    return dict(attempted=attempted, completed=completed, failed=failures, planned=len(trials))
