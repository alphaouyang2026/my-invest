from uuid import UUID

import pandas as pd

from app.research.artifacts import ArtifactStaging, ResearchArtifactReader
from app.research.evaluation import CrossSectionEvaluation, flatten_group_returns


def test_research_artifact_publishes_stable_json_and_parquet(tmp_path) -> None:
    run_id = UUID("22222222-2222-2222-2222-222222222222")
    scores = pd.DataFrame(
        [{"observation_date": "2025-01-31", "instrument_id": "alpha", "raw_score": 0.2}]
    )
    staged = ArtifactStaging(tmp_path / str(run_id)).write(
        run_id=run_id,
        tables={"scores": scores},
        summary={"weekly_ic_mean": 0.1},
        warnings=[{"code": "free_data_limit"}],
        runtime_identity={"pyqlib_version": "0.9.7"},
    )
    reader = ResearchArtifactReader(tmp_path)

    assert staged.manifest["schema_version"] == "1"
    assert staged.manifest["files"]["scores.parquet"]["rows"] == 1
    assert reader.summary(str(run_id))["weekly_ic_mean"] == 0.1
    assert reader.table(str(run_id), "scores").iloc[0]["instrument_id"] == "alpha"


def test_a_metrics_table_carrying_group_returns_survives_the_parquet_round_trip(tmp_path) -> None:
    """Quintile returns are keyed by group number, which Arrow cannot store: its
    struct fields and map keys are strings. Publishing used to fail on the very
    last step of a run that had already done all the work."""
    run_id = UUID("33333333-3333-3333-3333-333333333333")
    evaluation = CrossSectionEvaluation(
        factor_coverage=0.95,
        label_coverage=0.93,
        valid_factor_count=120,
        valid_label_count=118,
        paired_count=118,
        ic=0.04,
        rank_ic=0.05,
        group_returns={1: -0.01, 2: 0.0, 3: 0.005, 4: 0.01, 5: 0.02},
        long_short_return=0.03,
        unavailable_reasons=(),
    )
    row = {
        "observation_date": "2025-01-31",
        "frequency": "daily",
        "ic": evaluation.ic,
        "unavailable_reasons": list(evaluation.unavailable_reasons),
        **flatten_group_returns(evaluation.group_returns),
    }

    ArtifactStaging(tmp_path / str(run_id)).write(
        run_id=run_id,
        tables={"daily_metrics": pd.DataFrame([row])},
        summary={},
        warnings=[],
        runtime_identity={"pyqlib_version": "0.9.7"},
    )
    table = ResearchArtifactReader(tmp_path).table(str(run_id), "daily_metrics")

    assert table.iloc[0]["group_return_1"] == -0.01
    assert table.iloc[0]["group_return_5"] == 0.02
    assert list(table.iloc[0]["unavailable_reasons"]) == []
