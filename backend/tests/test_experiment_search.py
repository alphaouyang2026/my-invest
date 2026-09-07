"""No database fixtures: pure plans plus real LightGBM synthetic end-to-end trials."""
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.experiments.artifact_cache import digest, seal, verified, write_json
from app.experiments.search_plan import fold_plan, load_config, trial_plan
from app.experiments.qlib_lightgbm_direct import MatrixSegment
from app.experiments.search_runner import (
    code_identity, direct_config, environment_identity, run_search, successful, summarize_daily,
)
from app.experiments.search_report import block_interval, load_candidates, report, select_candidates


def config(tmp_path, **changes):
    values = dict(snapshot_id="11e402a7-27dc-4b36-8054-ba1f7bde2216", provider_root="unused",
                  train_start="2020-01-01", evaluation_start="2020-05-06", evaluation_end="2020-05-26",
                  valid_days=5, step=5, min_train_days=30, min_folds=2, purge_horizon=5,
                  feature_sets=["alpha158"], horizons=[5], train_windows=["expanding"],
                  stop_metrics=["l2", "rank_ic"],
                  model_params=dict(num_boost_round=6, early_stopping_rounds=2,
                                    min_data_in_leaf=20, num_leaves=7, max_depth=3)) | changes
    path = tmp_path / "config.json"
    write_json(path, values)
    return load_config(path)


def test_purge_maturity_and_fixed_validation(tmp_path):
    c = config(tmp_path, purge_horizon=20, horizons=[5,10,20], valid_days=10)
    cal = pd.bdate_range("2020-01-01", periods=150).strftime("%Y-%m-%d").tolist()
    plan = fold_plan(cal, c)
    for f in plan["folds"]:
        assert cal.index(f["valid"][1])-cal.index(f["valid"][0])+1 == 10
        assert cal.index(f["train"][1])+21 < cal.index(f["valid"][0])
        assert cal.index(f["valid"][1])+21 < cal.index(f["evaluation"][0])
    assert all(cal.index(d)+21 < len(cal) for d in plan["common_dates"])
    assert len(plan["common_dates"]) == len(set(plan["common_dates"]))


def test_insufficient_history_is_error_not_truncated_validation(tmp_path):
    c = config(tmp_path, valid_days=60, purge_horizon=20, train_windows=[252])
    with pytest.raises(ValueError, match="insufficient train history"):
        fold_plan(pd.bdate_range("2020-01-01", periods=150).date, c)


def test_structure_and_parameter_plans_deterministic(tmp_path):
    c = config(tmp_path, feature_sets=["alpha158","alpha360"], horizons=[5,10,20],
               purge_horizon=20, train_windows=["expanding",252])
    structural = trial_plan(c)
    assert len(structural) == 24
    c.update(mode="parameters", trials_per_structure=40)
    a, b = trial_plan(c, structural[:2]), trial_plan(c, structural[:2])
    assert a == b and len(a) == 80
    for t in a:
        p = t["resolved_params"]
        assert p["num_leaves"] <= 2**p["max_depth"]
    c.update(mode="replicate", seeds=[1,2,3])
    assert len(trial_plan(c, structural[:5])) == 15


def test_integrity_marker_catches_corruption(tmp_path):
    write_json(tmp_path / "metrics.json", {"n":1})
    seal(tmp_path, "id")
    assert verified(tmp_path, "id")
    write_json(tmp_path / "metrics.json", {"n":2})
    with pytest.raises(ValueError, match="checksum"):
        verified(tmp_path, "id")


def _direct_result(days):
    """What run_direct_prediction hands back, reduced to what a search records."""
    daily = pd.DataFrame(dict(
        fold=[1] * len(days), observation_date=days,
        rank_ic=np.linspace(0.1, 0.3, len(days)), ic=np.linspace(0.1, 0.3, len(days)),
    ))
    summary = {"folds": [
        {"fold": 1, "best_iteration": 5, "train_rows": 10, "valid_rows": 5, "test_rows": 4}
    ]}
    return SimpleNamespace(
        summary=summary, daily_ic=daily,
        predictions=pd.DataFrame(dict(datetime=days, score=np.arange(len(days), dtype=float))),
        feature_importance=pd.DataFrame(dict(fold=[1], feature_name=["f0"], gain=[1.0])),
    )


def _stub_direct(monkeypatch, calls, *, days=(), fail=False):
    def run(session, config, *, cache_root=None, artifact_dir=None):
        calls.append(config)
        if fail:
            raise ValueError("sensitive detail")
        models = Path(artifact_dir) / "models"
        models.mkdir(parents=True, exist_ok=True)
        (models / "fold-1.txt").write_text("model", encoding="utf-8")
        (Path(artifact_dir) / "learning_curves.parquet").write_bytes(
            pd.DataFrame(dict(fold=[1], value=[1.0])).to_parquet()
        )
        return _direct_result(list(days))

    monkeypatch.setattr("app.experiments.search_runner.run_direct_prediction", run)
    monkeypatch.setattr(
        "app.experiments.search_runner.get_sessionmaker", lambda: _NullSession, raising=False
    )


class _NullSession:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_trial_becomes_exactly_one_direct_run_config(tmp_path):
    """The whole of what a search contributes to a run.

    Every field a candidate was ranked on has to reach the entry point that will
    execute it, `purge_horizon` included -- without it, trials at different label
    horizons would be scored on different dates and stop being comparable.
    """
    c = config(tmp_path, horizons=[5], train_windows=[20], stop_metrics=["l2"])
    trial = trial_plan(c)[0]
    cal = pd.bdate_range("2020-01-01", periods=120).strftime("%Y-%m-%d").tolist()
    plan = fold_plan(cal, c)

    resolved = direct_config(c, plan, trial)

    assert resolved.feature_set == trial["feature_set"]
    assert resolved.label_horizon == trial["horizon"]
    assert resolved.purge_horizon == c["purge_horizon"]
    assert resolved.train_window == trial["train_window"]
    assert resolved.stop_metric == trial["stop_metric"]
    assert resolved.model_params == trial["model_params"]
    assert resolved.seed == trial["seed"]
    assert resolved.rolling_step == c["step"]
    assert [resolved.train.start.isoformat(), resolved.train.end.isoformat()] == plan["folds"][0]["train"]
    assert [resolved.valid.start.isoformat(), resolved.valid.end.isoformat()] == plan["folds"][0]["valid"]
    assert resolved.test.start.isoformat() == plan["folds"][0]["evaluation"][0]
    assert resolved.test.end.isoformat() == plan["folds"][-1]["evaluation"][1]


def test_end_to_end_two_trials_resume_report_and_selection(tmp_path, monkeypatch):
    c = config(tmp_path)
    cal = pd.bdate_range("2020-01-01",periods=120).strftime("%Y-%m-%d").tolist()
    plan = fold_plan(cal,c)
    trials = trial_plan(c)
    root = tmp_path / "experiment"
    write_json(root / "manifest.json", dict(experiment_id="test", config=c, trial_count=2,
               context=dict(code=code_identity(),environment=environment_identity())))
    write_json(root / "fold_plan.json", plan)
    write_json(root / "trial_plan.json",trials)
    calls = []
    _stub_direct(monkeypatch, calls, days=plan["common_dates"])

    status = run_search(root,None,max_trials=1)
    assert status["completed"]==1 and status["failed"]==0
    with pytest.raises(ValueError,match="Finish all"):
        select_candidates(root,[trials[0]["trial_id"]],tmp_path/"selected.json")
    status = run_search(root,None,resume=True)
    assert status["completed"]==2 and status["attempted"]==1
    assert len(calls)==2
    assert run_search(root,None,resume=True)["attempted"]==0
    r = report(root)
    assert r["status"]=="complete" and len(r["leaderboard"])==2
    for t in trials:
        target = successful(root,t)
        assert target is not None
        assert (target/"models/fold-1.txt").exists()
        assert (target/"learning_curves.parquet").exists()
    select_candidates(root,[trials[0]["trial_id"]],tmp_path/"selected.json")
    selected,source=load_candidates(tmp_path/"selected.json")
    assert source==root and selected==trials[:1]


def test_the_search_refuses_to_run_trials_side_by_side(tmp_path):
    """Each trial now loads its own segments; two at once double the ceiling."""
    c = config(tmp_path)
    root = tmp_path / "experiment"
    write_json(root / "manifest.json", dict(experiment_id="test", config=c, trial_count=0,
               context=dict(code=code_identity(), environment=environment_identity())))
    write_json(root / "fold_plan.json", {})
    write_json(root / "trial_plan.json", [])
    with pytest.raises(ValueError, match="workers must be 1"):
        run_search(root, None, workers=2)


def test_failed_trials_remain_in_report_and_retry(tmp_path, monkeypatch):
    c = config(tmp_path,stop_metrics=["l2"])
    root = tmp_path / "failed"
    trials = trial_plan(c)
    write_json(root/"manifest.json",dict(experiment_id="failed",config=c,trial_count=1,
               context=dict(code=code_identity(),environment=environment_identity())))
    write_json(root/"trial_plan.json",trials)
    write_json(root/"fold_plan.json",dict(common_dates=["2020-05-06"],purge_horizon=5,
               folds=[dict(fold=1, train=["2020-01-01","2020-02-01"],
                           valid=["2020-02-10","2020-03-01"],
                           evaluation=["2020-03-10","2020-03-20"])]))
    _stub_direct(monkeypatch, [], fail=True)
    assert run_search(root,None)["failed"]==1
    assert run_search(root,None,resume=True)["failed"]==1
    r = report(root)
    assert r["failed"]==[trials[0]["trial_id"]]
    contents = (root/"trials"/trials[0]["trial_id"]/"attempt-0001/failure.json").read_text()
    assert "sensitive detail" not in contents


def test_bootstrap_deterministic_and_insufficient_history():
    assert block_interval(np.arange(100),20)==block_interval(np.arange(100),20)
    assert block_interval(np.arange(30),20) is None


def test_without_best_fold_preserves_equal_date_weighting():
    daily=pd.DataFrame(dict(fold=[1,1,2,2,3],rank_ic=[.9,.9,.1,.1,-.2],ic=[.9,.9,.1,.1,-.2]))
    assert summarize_daily(daily,5)["without_best_fold"] == pytest.approx(0)


def test_unknown_config_field_rejected(tmp_path):
    with pytest.raises(ValueError,match="Unknown config"):
        config(tmp_path,validation_dayz=60)


def test_data_preparation_limits_are_validated_and_part_of_config(tmp_path):
    c = config(tmp_path, feature_batch_size=32, qlib_kernels=1, scan_batch_rows=2048)
    assert c["feature_batch_size"] == 32
    assert c["qlib_kernels"] == 1
    assert c["scan_batch_rows"] == 2048
    assert (c["qlib_kernels"], c["scan_batch_rows"]) == (1, 2048)
    for key in ("feature_batch_size", "qlib_kernels", "scan_batch_rows"):
        with pytest.raises(ValueError, match=key):
            config(tmp_path, **{key: 0})


def test_memory_monitor_uses_both_stop_conditions():
    from scripts.monitor_qlib_memory import GIB, over_limit
    assert not over_limit(int(5 * GIB), int(2 * GIB), stop_gib=6, reserve_gib=1.55)
    assert over_limit(int(6.1 * GIB), int(2 * GIB), stop_gib=6, reserve_gib=1.55)
    assert over_limit(int(5 * GIB), int(1.5 * GIB), stop_gib=6, reserve_gib=1.55)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_baseline_reference_accepts_windows_bom_and_log_prefix(tmp_path,encoding):
    from scripts.run_qlib_baseline import read_reference
    path=tmp_path/"result.txt"
    path.write_text('log prefix\n{\n  "summary": {"fold_count": 5}\n}\n',encoding=encoding)
    assert read_reference(path)=={"summary":{"fold_count":5}}




def test_the_search_owns_no_training_code() -> None:
    """The acceptance evidence that a candidate is transferable.

    A leaderboard says something about a model only if the model that earned the
    position and the model a later run produces are the same code. The strongest
    available form of that is for this module to contain no training at all: no
    LightGBM, no seam, no fold loop, no feature assembly.
    """
    import inspect

    from app.experiments import search_runner

    source = inspect.getsource(search_runner)
    for forbidden in ("import lightgbm", "lgb.train", "train_booster", "booster_training",
                      "read_features", "DataHandlerLP", "load_segment"):
        assert forbidden not in source, f"search_runner reintroduced {forbidden!r}"
    assert "run_direct_prediction" in source
