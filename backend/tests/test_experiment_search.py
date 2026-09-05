"""No database fixtures: pure plans plus real LightGBM synthetic end-to-end trials."""
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.experiments.search_plan import (
    digest, fold_plan, load_config, seal, trial_plan, verified, write_json,
)
from app.experiments.search_runner import (
    ShardedData, code_identity, daily_rank_ic, environment_identity, fit_model, run_search, successful,
    rank_evaluator, summarize_daily,
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


def test_rank_metric_is_equal_weight_per_day():
    y = np.r_[np.arange(100), np.arange(200)]
    p = np.r_[np.arange(100), -np.arange(200)]
    dates = np.r_[np.zeros(100), np.ones(200)]
    assert daily_rank_ic(p,y,dates) == pytest.approx(0)
    assert rank_evaluator(y, dates)(p) == pytest.approx(daily_rank_ic(p,y,dates))
    with pytest.raises(ValueError, match="No valid daily"):
        daily_rank_ic(np.ones(300),y,dates)


def test_integrity_marker_catches_corruption(tmp_path):
    write_json(tmp_path / "metrics.json", {"n":1})
    seal(tmp_path, "id")
    assert verified(tmp_path, "id")
    write_json(tmp_path / "metrics.json", {"n":2})
    with pytest.raises(ValueError, match="checksum"):
        verified(tmp_path, "id")


def synthetic_data(cal):
    rng = np.random.default_rng(7)
    idx = pd.MultiIndex.from_product([pd.to_datetime(cal), [str(i) for i in range(120)]],
                                     names=["datetime","instrument"])
    x = pd.DataFrame(rng.normal(size=(len(idx),6)), index=idx, columns=[f"f{i}" for i in range(6)])
    y = (x.f0*.8 + rng.normal(scale=.2,size=len(x))).rename("LABEL0").to_frame()
    frame = pd.concat({"feature":x,"label":y},axis=1)
    labels = y.reset_index().rename(columns={"datetime":"observation_date", "instrument":"instrument_id", "LABEL0":"label"})
    labels["observation_date"] = labels.observation_date.dt.date
    labels["label_reason"] = None
    return frame, frame.copy(), labels, {d.date():120 for d in pd.to_datetime(cal)}


@pytest.mark.parametrize("metric", ["l2","rank_ic"])
def test_real_lightgbm_stops_only_on_selected_validation_metric(tmp_path, metric):
    c = config(tmp_path, stop_metrics=[metric])
    trial = trial_plan(c)[0]
    cal = pd.bdate_range("2020-01-01",periods=8).strftime("%Y-%m-%d").tolist()
    learn,_,_,_ = synthetic_data(cal)
    train = learn.loc[pd.IndexSlice[:cal[4],:],:]
    valid = learn.loc[pd.IndexSlice[cal[5]:,:],:]
    model,curve,info = fit_model(train,valid,trial,2)
    values = curve[(curve.dataset=="valid") & (curve.metric==metric)]
    expected = values.loc[values.value.idxmin() if metric=="l2" else values.value.idxmax(),"iteration"]
    assert model.best_iteration == expected == info["best_iteration"]
    assert set(curve.dataset)=={"train","valid"}
    assert set(curve.metric)=={"l2","rank_ic"}


@pytest.mark.parametrize("workers", [1])
def test_end_to_end_two_trials_resume_report_and_selection(tmp_path, monkeypatch, workers):
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
    data = synthetic_data(cal)
    def prepare(*args):
        calls.append(args)
        return data
    monkeypatch.setattr("app.experiments.search_runner.prepare_data",prepare)
    status = run_search(root,None,max_trials=1)
    assert status["completed"]==1 and status["failed"]==0
    with pytest.raises(ValueError,match="Finish all"):
        select_candidates(root,[trials[0]["trial_id"]],tmp_path/"selected.json")
    status = run_search(root,None,resume=True,workers=workers)
    assert status["completed"]==2 and status["attempted"]==1
    assert len(calls)==2
    assert run_search(root,None,resume=True)["attempted"]==0
    r = report(root)
    assert r["status"]=="complete" and len(r["leaderboard"])==2
    assert all(row["eligible"] for row in r["leaderboard"])
    for t in trials:
        target = successful(root,t)
        assert target is not None
        assert (target/"models/fold-1.txt").exists()
        assert (target/"learning_curves.parquet").exists()
    select_candidates(root,[trials[0]["trial_id"]],tmp_path/"selected.json")
    selected,source=load_candidates(tmp_path/"selected.json")
    assert source==root and selected==trials[:1]


def test_sharded_search_rejects_parallel_workers(tmp_path):
    c = config(tmp_path)
    root = tmp_path / "experiment"
    write_json(root / "manifest.json", dict(experiment_id="test", config=c, trial_count=0,
               context=dict(code=code_identity(), environment=environment_identity())))
    write_json(root / "fold_plan.json", {})
    write_json(root / "trial_plan.json", [])
    with pytest.raises(ValueError, match="workers=1"):
        run_search(root, None, workers=2)


def test_failed_trials_remain_in_report_and_retry(tmp_path, monkeypatch):
    c = config(tmp_path,stop_metrics=["l2"])
    root = tmp_path / "failed"
    trials = trial_plan(c)
    write_json(root/"manifest.json",dict(experiment_id="failed",config=c,trial_count=1,
               context=dict(code=code_identity(),environment=environment_identity())))
    write_json(root/"trial_plan.json",trials)
    write_json(root/"fold_plan.json",dict(common_dates=["2020-05-06"],purge_horizon=5))
    def fail(*args):
        raise ValueError("sensitive detail")
    monkeypatch.setattr("app.experiments.search_runner.prepare_data",fail)
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
    c = config(tmp_path, feature_batch_size=32, qlib_kernels=1,
               scan_batch_rows=2048, cache_schema_version=2)
    assert c["feature_batch_size"] == 32
    assert c["qlib_kernels"] == 1
    assert c["scan_batch_rows"] == 2048
    assert c["cache_schema_version"] == 2
    for key in ("feature_batch_size", "qlib_kernels", "scan_batch_rows"):
        with pytest.raises(ValueError, match=key):
            config(tmp_path, **{key: 0})


def test_memory_monitor_uses_both_stop_conditions():
    from scripts.monitor_qlib_memory import GIB, over_limit
    assert not over_limit(int(5 * GIB), int(2 * GIB), stop_gib=6, reserve_gib=1.55)
    assert over_limit(int(6.1 * GIB), int(2 * GIB), stop_gib=6, reserve_gib=1.55)
    assert over_limit(int(5 * GIB), int(1.5 * GIB), stop_gib=6, reserve_gib=1.55)


def test_sharded_data_loads_one_segment_into_a_single_matrix(tmp_path):
    feature_root = tmp_path / "alpha360"
    names = ["F0", "F1"]
    rows = [
        ("2020-01-02", "a", 1.0, 2.0),
        ("2020-01-03", "a", 3.0, np.nan),
        ("2020-01-02", "b", 4.0, 5.0),
        ("2020-01-03", "b", 6.0, 7.0),
    ]
    for number, values in enumerate((rows[:2], rows[2:]), 1):
        folder = feature_root / f"batch-{number:04d}"
        folder.mkdir(parents=True)
        frame = pd.DataFrame(values, columns=["datetime", "instrument", *names])
        frame["datetime"] = pd.to_datetime(frame["datetime"])
        frame.to_parquet(folder / "features.parquet")
    labels = pd.DataFrame({
        "datetime": pd.to_datetime([r[0] for r in rows]),
        "instrument": [r[1] for r in rows],
        "raw_h5": [.1, .2, .3, np.nan],
        "learn_h5": [-1.73, -1.73, 1.73, np.nan],
    })
    labels.to_parquet(feature_root / "labels.parquet")
    data = ShardedData(feature_root, tuple(names), 5, scan_batch_rows=2, sizes={})
    segment = data.load_segment(["2020-01-02", "2020-01-03"], learning=True)
    assert segment.index.tolist() == [
        (pd.Timestamp("2020-01-02"), "a"),
        (pd.Timestamp("2020-01-02"), "b"),
        (pd.Timestamp("2020-01-03"), "a"),
    ]
    np.testing.assert_allclose(segment.features, [[1, 2], [4, 5], [3, np.nan]], equal_nan=True)
    np.testing.assert_allclose(segment.labels, [-1.73, 1.73, -1.73])


def test_sharded_data_rejects_duplicate_feature_rows(tmp_path):
    root = tmp_path / "data"
    batch = root / "batch-0001"
    batch.mkdir(parents=True)
    frame = pd.DataFrame({"datetime": pd.to_datetime(["2020-01-02"] * 2),
                          "instrument": ["a", "a"], "F0": [1.0, 1.0]})
    frame.to_parquet(batch / "features.parquet")
    pd.DataFrame({"datetime": pd.to_datetime(["2020-01-02"]), "instrument": ["a"],
                  "raw_h5": [.1], "learn_h5": [0.0]}).to_parquet(root / "labels.parquet")
    data = ShardedData(root, ("F0",), 5, scan_batch_rows=2, sizes={})
    with pytest.raises(ValueError, match="Duplicate feature row"):
        data.load_segment(["2020-01-02", "2020-01-02"], learning=True)


def test_sharded_search_trains_and_resume_is_a_noop(tmp_path, monkeypatch):
    c = config(tmp_path, stop_metrics=["l2"])
    cal = pd.bdate_range("2020-01-01", periods=120).strftime("%Y-%m-%d").tolist()
    plan = fold_plan(cal, c)
    learn, _, labels, sizes = synthetic_data(cal)
    feature_names = tuple(learn["feature"].columns)
    shard_root = tmp_path / "shards"
    features = learn["feature"].reset_index()
    for number, instruments in enumerate(([str(i) for i in range(60)],
                                           [str(i) for i in range(60, 120)]), 1):
        folder = shard_root / f"batch-{number:04d}"
        folder.mkdir(parents=True)
        features[features.instrument.isin(instruments)].to_parquet(folder / "features.parquet")
    narrow = labels.rename(columns={"observation_date": "datetime"})
    narrow["datetime"] = pd.to_datetime(narrow["datetime"])
    narrow["raw_h5"] = narrow["label"]
    narrow["learn_h5"] = narrow["label"]
    narrow[["datetime", "instrument_id", "raw_h5", "learn_h5"]].rename(
        columns={"instrument_id": "instrument"}).to_parquet(shard_root / "labels.parquet")
    data = ShardedData(shard_root, feature_names, 5, 128, sizes)
    root = tmp_path / "experiment"
    trials = trial_plan(c)
    write_json(root / "manifest.json", dict(experiment_id="test", config=c, trial_count=1,
               context=dict(code=code_identity(), environment=environment_identity())))
    write_json(root / "fold_plan.json", plan)
    write_json(root / "trial_plan.json", trials)
    monkeypatch.setattr("app.experiments.search_runner.prepare_data", lambda *args: data)
    assert run_search(root, None) == dict(attempted=1, completed=1, failed=0, planned=1)
    pointer = (root / "trials" / trials[0]["trial_id"] / "success.json").read_bytes()
    attempts = list((root / "trials" / trials[0]["trial_id"]).glob("attempt-*"))
    assert run_search(root, None, resume=True)["attempted"] == 0
    assert (root / "trials" / trials[0]["trial_id"] / "success.json").read_bytes() == pointer
    assert list((root / "trials" / trials[0]["trial_id"]).glob("attempt-*")) == attempts


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_baseline_reference_accepts_windows_bom_and_log_prefix(tmp_path,encoding):
    from scripts.run_qlib_baseline import read_reference
    path=tmp_path/"result.txt"
    path.write_text('log prefix\n{\n  "summary": {"fold_count": 5}\n}\n',encoding=encoding)
    assert read_reference(path)=={"summary":{"fold_count":5}}


def test_real_qlib_preparation_reuses_raw_and_universe_cache(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from app.experiments.search_runner import prepare_data
    from app.research.feature_sets import FeatureSet, FeatureSpec
    c = config(tmp_path, horizons=[5,10],purge_horizon=10, feature_batch_size=200)
    cal = pd.bdate_range("2020-01-01",periods=120)
    plan = fold_plan(cal.date,c)
    root = tmp_path/"cached"
    write_json(root/"manifest.json",dict(experiment_id="cache-test",config=c))
    write_json(root/"fold_plan.json",plan)
    spec = FeatureSet("toy","1","test",(FeatureSpec("CLOSE0","$close",("close",),1),))
    monkeypatch.setattr("app.experiments.qlib_lightgbm_direct._resolve_feature_set",lambda _:spec)
    monkeypatch.setattr("app.db.session.get_sessionmaker",lambda:lambda:nullcontext(None))
    monkeypatch.setattr("app.experiments.qlib_lightgbm_direct._load_and_validate_snapshot",
                        lambda *args:SimpleNamespace(calendar_publication_id="test"))
    monkeypatch.setattr("app.services.calendar_port.SessionCalendarPort",lambda *args:None)
    calls = dict(pool=0,features=0)
    def pool(*args):
        calls["pool"]+=1
        return ({d.date():{str(i) for i in range(120)} for d in cal},
                {d.date():120 for d in cal},{},[],"pool")
    def raw(*args,**kwargs):
        calls["features"]+=1
        index=pd.MultiIndex.from_product([kwargs["instruments"],cal],names=["instrument","datetime"])
        rng=np.random.default_rng(9)
        return pd.DataFrame({"$close":100+np.cumsum(rng.normal(size=len(index)))},index=index)
    monkeypatch.setattr("app.experiments.qlib_lightgbm_direct._build_universe",pool)
    monkeypatch.setattr("app.research.qlib_runtime.read_features",raw)
    provider=SimpleNamespace(path=tmp_path,manifest=SimpleNamespace(
        coverage_start=cal[0].date(),coverage_end=cal[-1].date(),calendar=list(cal.date)))
    first=prepare_data(root,provider,"alpha158",5)
    second=prepare_data(root,provider,"alpha158",10)
    repeated=prepare_data(root,provider,"alpha158",5)
    other=tmp_path/"next-stage"
    write_json(other/"manifest.json",dict(experiment_id="different-trials",config=c))
    write_json(other/"fold_plan.json",plan)
    prepare_data(other,provider,"alpha158",5)
    assert calls==dict(pool=1,features=1)
    assert isinstance(first, ShardedData) and isinstance(repeated, ShardedData)
    assert first.root == repeated.root == second.root
    assert first.horizon == 5 and second.horizon == 10
    loaded = first.load_segment(plan["folds"][0]["train"], learning=True)
    assert loaded.features.shape[1] == 1
    (first.root / "batch-9999").mkdir()
    with pytest.raises(ValueError, match="unexpected batches"):
        prepare_data(root, provider, "alpha158", 5)


def test_early_pool_filter_preserves_future_out_of_pool_prices(tmp_path):
    from app.experiments.qlib_lightgbm_direct import _build_dataset, DateRange
    from datetime import date
    days=pd.bdate_range("2020-01-01",periods=12)
    index=pd.MultiIndex.from_product([["stock"],days],names=["instrument","datetime"])
    raw=pd.DataFrame({"$close":np.arange(100.,112.)},index=index)
    provider=SimpleNamespace(path=tmp_path,manifest=SimpleNamespace(
        calendar=list(days.date),coverage_start=days[0].date(),coverage_end=days[-1].date()))
    feature=SimpleNamespace(expressions=("$close",),column_names=("CLOSE0",))
    config=SimpleNamespace(train=DateRange(days[0].date(),days[0].date()),label_horizon=5)
    assembled=_build_dataset(provider,feature,config,{days[0].date():{"stock"}},raw_features=raw)
    assert len(assembled.raw_labels)==1
    assert assembled.raw_labels.iloc[0]["label"] == pytest.approx(106/101-1)
