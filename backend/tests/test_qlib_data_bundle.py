from datetime import date
from decimal import Decimal
from uuid import UUID

import pandas as pd
import pytest

from app.research.bundle import BundleBar, build_feature_frames, write_native_bundle
from app.research.qlib_runtime import analyze_signals


def test_native_bundle_is_readable_by_locked_qlib_provider(tmp_path) -> None:
    qlib = pytest.importorskip("qlib")
    from qlib.config import REG_CN
    from qlib.data import D

    instrument_id = "11111111-1111-1111-1111-111111111111"
    calendar = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8)]
    frame = pd.DataFrame(
        {
            "close": [100.0, 110.0, 121.0],
            "factor": [1.0, 1.0, 1.0],
        },
        index=calendar,
    )

    write_native_bundle(tmp_path, calendar=calendar, features=[(instrument_id, frame)])
    qlib.init(
        provider_uri=str(tmp_path),
        region=REG_CN,
        expression_cache=None,
        dataset_cache=None,
        clear_mem_cache=True,
    )
    result = D.features(
        D.instruments(market="all"),
        ["$close", "$factor", "Ref($close, 1)/Ref($close, 2)-1"],
        start_time="2025-01-06",
        end_time="2025-01-08",
        freq="day",
    )

    assert qlib.__version__ == "0.9.7"
    assert result.loc[(instrument_id, pd.Timestamp("2025-01-08")), "$close"] == pytest.approx(121.0)
    assert result.loc[(instrument_id, pd.Timestamp("2025-01-08")), "$factor"] == pytest.approx(1.0)
    assert result.loc[
        (instrument_id, pd.Timestamp("2025-01-08")),
        "Ref($close, 1)/Ref($close, 2)-1",
    ] == pytest.approx(0.1)


def test_bundle_fields_derive_cumulative_factor_and_hide_untradable_research_prices() -> None:
    instrument_id = UUID("11111111-1111-1111-1111-111111111111")
    days = [date(2025, 1, 6), date(2025, 1, 7)]
    frames = build_feature_frames(
        days,
        [
            BundleBar(
                instrument_id=instrument_id,
                trade_date=days[0],
                raw_open=Decimal("50"),
                raw_close=Decimal("50"),
                adjusted_open=Decimal("100"),
                adjusted_close=Decimal("100"),
                quality_status="ok",
            ),
            BundleBar(
                instrument_id=instrument_id,
                trade_date=days[1],
                raw_open=Decimal("51"),
                raw_close=Decimal("52"),
                adjusted_open=Decimal("102"),
                adjusted_close=Decimal("104"),
                quality_status="untradable",
                quality_rules=("missing_volume",),
            ),
        ],
    )
    frame = frames[str(instrument_id)]

    assert frame.loc[days[0], "factor"] == pytest.approx(2.0)
    assert frame.loc[days[0], "close"] == pytest.approx(100.0)
    assert frame.loc[days[1], "rawclose"] == pytest.approx(52.0)
    assert pd.isna(frame.loc[days[1], "close"])
    assert frame.loc[days[1], "quality_status"] == 2.0
    assert frame.loc[days[1], "quality_missing_volume"] == 1.0


def test_locked_qlib_sig_ana_record_generates_ic_artifacts() -> None:
    index = pd.MultiIndex.from_product(
        [
            [pd.Timestamp("2025-01-06"), pd.Timestamp("2025-01-13")],
            ["a", "b", "c"],
        ],
        names=["datetime", "instrument"],
    )
    predictions = pd.DataFrame({"score": [1.0, 2.0, 3.0, 3.0, 2.0, 1.0]}, index=index)
    labels = pd.DataFrame({"label": [0.1, 0.2, 0.3, 0.3, 0.1, 0.2]}, index=index)

    result = analyze_signals(predictions, labels)

    assert result["ic"].tolist() == pytest.approx([1.0, 0.5])
    assert result["rank_ic"].tolist() == pytest.approx([1.0, 0.5])
    assert result["metrics"]["IC"] == pytest.approx(0.75)
