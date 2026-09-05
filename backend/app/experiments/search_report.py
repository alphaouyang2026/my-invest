"""Read verified artifacts; report uncertainty without calling development a blind test."""
from __future__ import annotations

from pathlib import Path
import numpy as np
import pandas as pd

from app.experiments.search_plan import digest, read_json, write_json
from app.experiments.search_runner import successful


def block_interval(values, block: int, seed=42, draws=1000):
    values = np.asarray(values, dtype=float)
    if len(values) < 2*block or np.isfinite(values).sum() < 2*block:
        return None
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(draws):
        starts = rng.integers(0, len(values)-block+1, size=int(np.ceil(len(values)/block)))
        sample = np.concatenate([values[i:i+block] for i in starts])[:len(values)]
        if np.isfinite(sample).any():
            means.append(float(np.nanmean(sample)))
    return [float(x) for x in np.quantile(means, [.025, .975])] if means else None


def report(root: Path) -> dict:
    manifest = read_json(root / "manifest.json")
    plan = read_json(root / "fold_plan.json")
    rows, daily_by_structure, incomplete, failed = [], {}, [], []
    for trial in read_json(root / "trial_plan.json"):
        target = successful(root, trial)
        if target is None:
            incomplete.append(trial["trial_id"])
            if list((root / "trials" / trial["trial_id"]).glob("attempt-*/failure.json")):
                failed.append(trial["trial_id"])
            continue
        metrics = read_json(target / "metrics.json")
        daily = pd.read_parquet(target / "daily_metrics.parquet").sort_values("observation_date")
        daily["observation_date"] = daily["observation_date"].astype(str)
        series = daily.set_index("observation_date")["rank_ic"].reindex(plan["common_dates"])
        structure = {k: v for k, v in trial.items() if k not in {"seed", "resolved_params", "trial_id"}}
        group = digest(structure)[:20]
        daily_by_structure.setdefault(group, []).append(series.rename(str(trial["seed"])))
        row = {k:v for k,v in metrics.items() if k not in {"folds", "fold_rank_ic"}}
        row.update({k: trial[k] for k in ("trial_id", "feature_set", "horizon", "train_window", "stop_metric", "seed")})
        row.update(structure_id=group, fold_rank_ic=metrics["fold_rank_ic"],
                   interval=block_interval(series, manifest["config"]["purge_horizon"]))
        rows.append(row)
    rows.sort(key=lambda r: (not r["eligible"], -(r["rank_ic_mean"] if r["rank_ic_mean"] is not None else -1)))
    robustness = []
    for group, series in daily_by_structure.items():
        frame = pd.concat(series, axis=1)
        # Seeds are replications, not extra dates: bootstrap the per-date seed mean.
        values = frame.mean(axis=1).to_numpy()
        block = manifest["config"]["purge_horizon"]
        seed_means = frame.mean()
        robustness.append(dict(structure_id=group, seeds=list(frame.columns),
                               mean_rank_ic=float(seed_means.mean()) if seed_means.notna().any() else None,
                               seed_mean_std=float(seed_means.std()) if len(seed_means)>1 and seed_means.notna().all() else None,
                               interval=block_interval(values, block),
                               longer_block_interval=block_interval(values, 2*block)))
    result = dict(experiment_id=manifest["experiment_id"], completed=len(rows),
                  planned=manifest["trial_count"], incomplete=incomplete, failed=failed,
                  status="complete" if not incomplete else "partial_not_for_selection",
                  leaderboard=rows, robustness=robustness)
    write_json(root / "report.json", result)
    pd.DataFrame(rows).to_csv(root / "leaderboard.csv", index=False)
    lines = ["# Qlib + LightGBM 开发期对照实验", "",
             f"完成 {len(rows)}/{manifest['trial_count']}；失败 {len(failed)}。状态：{result['status']}", "",
             "本报告不是独立盲测，不代表可交易收益。候选在本时期被选择，存在多重搜索偏差。", "",
             f"共同成熟日期 {len(plan['common_dates'])}；purge horizon={plan['purge_horizon']}。", "",
             "| trial | 特征 | label | Rank IC | 覆盖 | 最差 Fold |", "|---|---|---:|---:|---:|---:|"]
    for row in rows:
        fmt = lambda v: "NA" if v is None else f"{v:.6f}"
        lines.append(f"| {row['trial_id']} | {row['feature_set']} | {row['horizon']} | "
                     f"{fmt(row['rank_ic_mean'])} | {row['coverage']:.1%} | {fmt(row['worst_fold'])} |")
    lines += ["", "## 复验说明", "", "区块 bootstrap 保留时间依赖；样本不足两倍区块长度时区间为 null。",
              "不同 seed 先按同一天平均，不把重复 seed 当作更多独立交易日。",
              "详细逐 Fold、去掉最好 Fold、多种子及双区块长度结果见 report.json。",
              "统计接近时优先选择简单且跨 Fold 稳定的配置；best_iteration=1 是诊断项，不自动淘汰。",
              "统一交易回测及新时期盲测需另行执行，当前脚本不模拟或发布交易。"]
    (root / "report.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    return result


def select_candidates(root: Path, ids: list[str], destination: Path):
    status = report(root)
    if status["incomplete"]:
        raise ValueError("Finish all planned trials before selecting candidates")
    trials = {t["trial_id"]:t for t in read_json(root / "trial_plan.json")}
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("Specify unique trial IDs")
    for identity in ids:
        if identity not in trials or successful(root, trials[identity]) is None:
            raise ValueError(f"Unverified trial: {identity}")
        target = successful(root, trials[identity])
        if not read_json(target / "metrics.json")["eligible"]:
            raise ValueError("Cannot select a candidate with missing evaluation dates")
    write_json(destination, dict(source_root=str(root.resolve()),
                                experiment_id=status["experiment_id"], trial_ids=ids))


def load_candidates(path: Path):
    selection = read_json(path)
    root = Path(selection["source_root"])
    manifest = read_json(root / "manifest.json")
    if manifest["experiment_id"] != selection["experiment_id"]:
        raise ValueError("Candidate source identity changed")
    plans = read_json(root / "trial_plan.json")
    if any(successful(root, t) is None for t in plans):
        raise ValueError("Candidate source search is incomplete")
    by_id = {t["trial_id"]:t for t in plans}
    selected = []
    for identity in selection["trial_ids"]:
        trial = by_id[identity]
        if not read_json(successful(root, trial) / "metrics.json")["eligible"]:
            raise ValueError("Ineligible candidate")
        selected.append(trial)
    return selected, root
