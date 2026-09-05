"""Replay the original direct experiment and compare its captured result, without publishing."""
from __future__ import annotations

import argparse
import json
import uuid
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from app.experiments.search_plan import digest, exclusive_lock, file_digest, read_json, seal, verified, write_json


def read_reference(path: Path):
    encoded = path.read_bytes()
    encoding = "utf-16" if encoded.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    raw = encoded.decode(encoding)
    import re
    starts = list(re.finditer(r'^\{\s*"summary"\s*:', raw, re.MULTILINE))
    if not starts:
        raise ValueError("Reference contains no summary JSON")
    return json.loads(raw[starts[-1].start():])


def compare(reference, result, tolerance=1e-9):
    issues = []
    for key in ("snapshot_id", "provider_logical_checksum", "feature_definition_checksum",
                "stock_pool_policy_fingerprint", "label_horizon", "folds", "best_iterations"):
        if reference["summary"][key] != result.summary[key]:
            issues.append(key)
    for key in ("test_ic_mean", "test_icir", "test_rank_ic_mean", "test_rank_icir"):
        if not np.isclose(reference["summary"][key], result.summary[key], atol=tolerance, rtol=0):
            issues.append(key)
    old = pd.DataFrame(reference["predictions"])
    new = result.predictions.copy()
    for frame in (old, new):
        frame["datetime"] = frame["datetime"].astype(str)
        frame["instrument_id"] = frame["instrument_id"].astype(str)
    keys = ["fold", "datetime", "instrument_id"]
    old, new = old.set_index(keys).sort_index(), new.set_index(keys).sort_index()
    if not old.index.equals(new.index) or not np.allclose(old["score"], new["score"], atol=tolerance, rtol=0):
        issues.append("prediction_scores")
    return dict(matches=not issues, differences=issues, absolute_tolerance=tolerance)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--reference", type=Path, required=True)
    p.add_argument("--output", type=Path, default=Path("var/experiment-baseline"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--resume", action="store_true")
    args = p.parse_args(argv)
    config, reference = read_json(args.config), read_reference(args.reference)
    from app.experiments.qlib_lightgbm_direct import DateRange, DirectPredictionConfig, run_direct_prediction
    from app.experiments.search_runner import code_identity, environment_identity, parquet
    from app.db.session import get_sessionmaker
    direct = DirectPredictionConfig(
        snapshot_id=uuid.UUID(config["snapshot_id"]), feature_set=config["feature_set"],
        train=DateRange(*map(date.fromisoformat, config["train"])),
        valid=DateRange(*map(date.fromisoformat, config["valid"])),
        test=DateRange(*map(date.fromisoformat, config["test"])),
        provider_root=Path(config["provider_root"]), seed=config["seed"],
        num_threads=config["num_threads"], label_horizon=config["label_horizon"], rolling_step=config["rolling_step"])
    identity = digest(dict(config=config, reference=file_digest(args.reference), code=code_identity(),
                           environment=environment_identity()))
    root = args.output / identity[:20]
    print(root)
    if args.dry_run:
        print("Original baseline replay planned; no training performed")
        return 0
    with exclusive_lock(root):
        if verified(root, identity):
            if not args.resume:
                raise ValueError("Baseline exists; use --resume")
            return 0 if read_json(root / "comparison.json")["matches"] else 1
        write_json(root / "config.json", config)
        with get_sessionmaker()() as session:
            result = run_direct_prediction(session, direct)
        comparison = compare(reference, result)
        write_json(root / "summary.json", result.summary)
        write_json(root / "comparison.json", comparison)
        for name in ("daily_ic", "predictions", "feature_importance"):
            parquet(root / f"{name}.parquet", getattr(result, name))
        seal(root, identity)
        print(comparison)
        return 0 if comparison["matches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
