"""Thin shared command-line adapters. Expensive training always requires a run entry point."""
from __future__ import annotations

import argparse
from pathlib import Path

from app.experiments.artifact_cache import read_json
from app.experiments.search_plan import load_config


def main(action: str, argv=None) -> int:
    parser = argparse.ArgumentParser(description=f"Qlib experiment: {action}")
    if action in {"plan", "run"}:
        parser.add_argument("--config", type=Path, required=True)
        parser.add_argument("--output", type=Path, default=Path("var/experiment-search"))
        parser.add_argument("--dry-run", action="store_true", help="Plan only; no feature calculation or training")
        parser.add_argument("--max-trials", type=int)
        parser.add_argument("--resume", action="store_true")
        parser.add_argument("--workers", type=int, choices=[1], default=1,
                            help="Serial only: each trial loads its own segments from the "
                                 "shared feature cache, so two at once double the peak")
    else:
        parser.add_argument("--experiment", type=Path, required=True)
        if action == "evaluate":
            parser.add_argument("--select", nargs="+", help="Explicit verified trial IDs to promote")
            parser.add_argument("--candidates-out", type=Path)
    args = parser.parse_args(argv)
    if action in {"plan", "run"}:
        from app.experiments.search_runner import plan_experiment, run_search
        from app.experiments.search_report import load_candidates, report
        config = load_config(args.config)
        if args.max_trials is not None and args.max_trials < 1:
            parser.error("--max-trials must be positive")
        candidates, source = None, None
        if config["mode"] != "structure":
            if not config.get("candidates"):
                parser.error("This mode requires candidates in config")
            path = Path(config["candidates"])
            if not path.is_absolute():
                path = args.config.parent / path
            candidates, source = load_candidates(path)
        root, provider = plan_experiment(config, args.output, candidates)
        if source:
            if read_json(source / "fold_plan.json") != read_json(root / "fold_plan.json"):
                raise ValueError("Candidate stage must retain exactly the source fold plan")
            old, new = read_json(source / "manifest.json"), read_json(root / "manifest.json")
            if old["context"] != new["context"]:
                raise ValueError("Candidate stage data/code/environment changed")
        print(root)
        if action == "plan" or args.dry_run:
            plan = read_json(root / "fold_plan.json")
            print(f"{len(plan['folds'])} folds, {len(plan['common_dates'])} mature dates, "
                  f"{len(read_json(root / 'trial_plan.json'))} trials; no training performed")
            return 0
        status = run_search(root, provider, resume=args.resume, max_trials=args.max_trials, workers=args.workers)
        report(root)
        print(status)
        return 1 if status["failed"] else 0
    from app.experiments.search_report import report, select_candidates
    result = report(args.experiment)
    if action == "evaluate" and args.select:
        if not args.candidates_out:
            parser.error("--select requires --candidates-out")
        select_candidates(args.experiment, args.select, args.candidates_out)
    print(f"{result['status']}: {result['completed']}/{result['planned']}")
    return 0
