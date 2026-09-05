"""Pure, deterministic planning and auditable filesystem artifacts for searches."""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import random
from contextlib import contextmanager
from datetime import date
from pathlib import Path


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                     allow_nan=False).encode()).hexdigest()


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, default=str,
                                    allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@contextmanager
def exclusive_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".running.lock"
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lock.unlink()


def seal(root: Path, identity: str) -> None:
    files = {str(p.relative_to(root)): file_digest(p) for p in sorted(root.rglob("*"))
             if p.is_file() and p.name not in {"complete.json", ".running.lock"} and not p.name.endswith(".tmp")}
    write_json(root / "complete.json", {"identity": identity, "files": files})


def verified(root: Path, identity: str) -> bool:
    marker = root / "complete.json"
    if not marker.exists():
        return False
    value = read_json(marker)
    if value["identity"] != identity:
        raise ValueError(f"Artifact identity mismatch: {root}")
    for name, checksum in value["files"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file() or file_digest(path) != checksum:
            raise ValueError(f"Artifact checksum mismatch: {name}")
    return True


def load_config(path: Path) -> dict:
    config = read_json(path)
    allowed = {"snapshot_id", "provider_root", "train_start", "evaluation_start",
               "evaluation_end", "valid_days", "step", "min_train_days", "min_folds",
               "purge_horizon", "feature_sets", "horizons", "train_windows", "stop_metrics",
               "seed", "search_seed", "num_threads", "model_params", "mode", "candidates",
               "trials_per_structure", "seeds", "feature_batch_size", "qlib_kernels",
               "scan_batch_rows", "cache_schema_version"}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(f"Unknown config fields: {sorted(unknown)}")
    for key in ("snapshot_id", "provider_root", "train_start", "evaluation_start", "evaluation_end"):
        if key not in config:
            raise ValueError(f"Missing {key}")
    defaults = dict(valid_days=60, step=20, min_train_days=252, min_folds=3,
                    purge_horizon=20, feature_sets=["alpha158", "alpha360"],
                    horizons=[5, 10, 20], train_windows=["expanding", 252],
                    stop_metrics=["l2", "rank_ic"], seed=20260829, search_seed=42,
                    num_threads=2, model_params={}, mode="structure",
                    trials_per_structure=40, seeds=[20260829, 20260830, 20260831],
                    feature_batch_size=64, qlib_kernels=2, scan_batch_rows=4096,
                    cache_schema_version=2)
    config = defaults | config
    for key in ("valid_days", "step", "min_train_days", "min_folds", "purge_horizon",
                "num_threads", "trials_per_structure", "feature_batch_size", "qlib_kernels",
                "scan_batch_rows", "cache_schema_version"):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError(f"{key} must be a positive integer")
    if config["mode"] not in ("structure", "parameters", "replicate"):
        raise ValueError("mode must be structure, parameters or replicate")
    if not config["horizons"] or any(type(h) is not int or h < 1 for h in config["horizons"]):
        raise ValueError("horizons must contain positive integers")
    if max(config["horizons"]) > config["purge_horizon"]:
        raise ValueError("purge_horizon must cover every label horizon")
    if not config["feature_sets"] or set(config["feature_sets"]) - {"alpha158", "alpha360"}:
        raise ValueError("Unsupported feature set")
    if not config["stop_metrics"] or set(config["stop_metrics"]) - {"l2", "rank_ic"}:
        raise ValueError("Unsupported stopping metric")
    if not config["train_windows"] or any(w != "expanding" and (type(w) is not int or w < 1)
                                         for w in config["train_windows"]):
        raise ValueError("train_windows must contain expanding or positive day counts")
    for key in ("train_start", "evaluation_start", "evaluation_end"):
        date.fromisoformat(config[key])
    if not config["train_start"] < config["evaluation_start"] <= config["evaluation_end"]:
        raise ValueError("Invalid date order")
    return config


def fold_plan(calendar, config: dict) -> dict:
    days = sorted({str(d) for d in calendar})
    for key in ("train_start", "evaluation_start", "evaluation_end"):
        if config[key] not in days:
            raise ValueError(f"{key} is not a covered trading day")
    first = days.index(config["train_start"])
    start = days.index(config["evaluation_start"])
    end = days.index(config["evaluation_end"])
    lookahead = config["purge_horizon"] + 1
    mature_end = min(end, len(days) - lookahead - 1)
    if mature_end < start:
        raise ValueError("No common mature evaluation dates")
    fixed = [w for w in config["train_windows"] if isinstance(w, int)]
    required = max([config["min_train_days"], *fixed])
    folds = []
    for index in range(start, mature_end + 1, config["step"]):
        ve = index - lookahead - 1
        vs = ve - config["valid_days"] + 1
        te = vs - lookahead - 1
        if te - first + 1 < required:
            raise ValueError(f"Fold {len(folds)+1}: insufficient train history; need {required} days "
                             f"before a {config['valid_days']}-day validation and two purges")
        stop = min(index + config["step"] - 1, mature_end)
        folds.append(dict(fold=len(folds)+1, train=[days[first], days[te]],
                          train_days=te-first+1, valid=[days[vs], days[ve]],
                          valid_days=config["valid_days"], evaluation=[days[index], days[stop]],
                          evaluation_days=stop-index+1))
    if len(folds) < config["min_folds"]:
        raise ValueError(f"Only {len(folds)} folds; min_folds={config['min_folds']}. Obtain more data.")
    return dict(folds=folds, calendar=days, common_dates=days[start:mature_end+1],
                excluded_immature_dates=days[mature_end+1:end+1], purge_horizon=config["purge_horizon"])


def sample_params(rng: random.Random) -> dict:
    depth = rng.randint(3, 8)
    log = lambda a, b: math.exp(rng.uniform(math.log(a), math.log(b)))
    return dict(learning_rate=log(.01, .1), max_depth=depth,
                num_leaves=rng.choice([n for n in (7, 15, 31, 63) if n <= 2**depth]),
                min_data_in_leaf=round(log(100, 2000)), feature_fraction=rng.uniform(.5, 1),
                bagging_fraction=rng.uniform(.6, 1), bagging_freq=1,
                lambda_l1=rng.choice([0, .1, 1, 3, 10, 30]), lambda_l2=log(1, 100),
                num_boost_round=2000, early_stopping_rounds=100)


def trial_plan(config: dict, candidates: list[dict] | None = None) -> list[dict]:
    from app.research.model_definition import resolve_model_params
    specs = []
    if config["mode"] == "structure":
        for feature, horizon, window, metric in itertools.product(
            config["feature_sets"], config["horizons"], config["train_windows"], config["stop_metrics"]
        ):
            specs.append(dict(feature_set=feature, horizon=horizon, train_window=window,
                              stop_metric=metric, seed=config["seed"], model_params=config["model_params"]))
    else:
        if not candidates:
            raise ValueError("Select verified candidates before parameter search or replication")
        rng = random.Random(config["search_seed"])
        for candidate in candidates:
            base = {k: candidate[k] for k in ("feature_set", "horizon", "train_window", "stop_metric")}
            if config["mode"] == "parameters":
                for n in range(config["trials_per_structure"]):
                    params = (candidate["model_params"] | dict(num_boost_round=2000, early_stopping_rounds=100)
                              if n == 0 else sample_params(rng))
                    specs.append(base | dict(seed=config["seed"], model_params=params))
            else:
                for seed in config["seeds"]:
                    specs.append(base | dict(seed=seed, model_params=candidate["model_params"]))
    output = {}
    for spec in specs:
        if spec["feature_set"] not in config["feature_sets"] or spec["horizon"] not in config["horizons"]:
            raise ValueError("Candidate outside configured feature/horizon space")
        if spec["train_window"] not in config["train_windows"] or spec["stop_metric"] not in config["stop_metrics"]:
            raise ValueError("Candidate outside configured window/metric space")
        resolved = resolve_model_params(spec["model_params"], seed=spec["seed"])
        identity = digest(spec | {"resolved_params": resolved})[:20]
        output[identity] = spec | dict(trial_id=identity, resolved_params=resolved)
    return list(output.values())
