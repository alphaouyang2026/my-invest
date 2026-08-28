from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import numpy as np
import pandas as pd

QUALITY_STATUS_CODES = {"ok": 0.0, "excluded": 1.0, "untradable": 2.0}


@dataclass(frozen=True)
class BundleBar:
    instrument_id: UUID
    trade_date: date
    raw_open: Decimal | None = None
    raw_high: Decimal | None = None
    raw_low: Decimal | None = None
    raw_close: Decimal | None = None
    raw_volume: Decimal | None = None
    adjusted_open: Decimal | None = None
    adjusted_high: Decimal | None = None
    adjusted_low: Decimal | None = None
    adjusted_close: Decimal | None = None
    adjusted_volume: Decimal | None = None
    trading_value: Decimal | None = None
    adjustment_factor: Decimal | None = None
    quality_status: str = "ok"
    quality_rules: tuple[str, ...] = ()


@dataclass(frozen=True)
class BundleContents:
    """What a written bundle actually holds, for the manifest to describe.

    `instruments` lists what reached `features/`, which is narrower than what
    was offered: a security whose every bar is untradable produces a frame of
    nothing but NaN, and writing it would claim coverage the bundle does not
    have.
    """

    instruments: tuple[str, ...]
    fields: tuple[str, ...]


def _number(value: Decimal | None) -> float:
    return np.nan if value is None else float(value)


def build_instrument_frame(
    calendar_index: pd.Index,
    bars: Sequence[BundleBar],
    *,
    quality_fields: Sequence[str],
) -> pd.DataFrame:
    """Map one security's bar facts to Qlib fields without filling gaps.

    `quality_fields` is passed in rather than derived here: every security in a
    bundle must carry the same columns, and one security's bars cannot know
    which rules the rest of the snapshot fired.
    """
    records: list[dict] = []
    for bar in bars:
        untradable = bar.quality_status == "untradable"
        research = lambda value: np.nan if untradable else _number(value)
        raw_close = _number(bar.raw_close)
        adjusted_close = research(bar.adjusted_close)
        factor = (
            adjusted_close / raw_close
            if not np.isnan(adjusted_close) and not np.isnan(raw_close) and raw_close > 0
            else np.nan
        )
        row = {
            "trade_date": bar.trade_date,
            "open": research(bar.adjusted_open),
            "high": research(bar.adjusted_high),
            "low": research(bar.adjusted_low),
            "close": adjusted_close,
            "volume": research(bar.adjusted_volume),
            "factor": factor,
            "rawopen": _number(bar.raw_open),
            "rawhigh": _number(bar.raw_high),
            "rawlow": _number(bar.raw_low),
            "rawclose": raw_close,
            "rawvolume": _number(bar.raw_volume),
            "trading_value": _number(bar.trading_value),
            "adjustment_event": _number(bar.adjustment_factor),
            "quality_status": QUALITY_STATUS_CODES.get(bar.quality_status, np.nan),
        }
        row.update({f"quality_{rule}": float(rule in bar.quality_rules) for rule in quality_fields})
        records.append(row)

    frame = pd.DataFrame.from_records(records).set_index("trade_date")
    return frame.reindex(calendar_index).astype("float32")


def build_feature_frames(
    calendar: Sequence[date],
    bars: Sequence[BundleBar],
) -> dict[str, pd.DataFrame]:
    """Whole-collection convenience over `build_instrument_frame`.

    Holds every security at once, so it is for small inputs and tests. The
    exporter streams one security at a time instead — a snapshot's worth of
    bars does not fit in memory three times over.
    """
    quality_fields = sorted({rule for bar in bars for rule in bar.quality_rules})
    grouped: dict[str, list[BundleBar]] = {}
    for bar in bars:
        grouped.setdefault(str(bar.instrument_id), []).append(bar)

    calendar_index = pd.Index(calendar)
    return {
        instrument_id: build_instrument_frame(
            calendar_index, instrument_bars, quality_fields=quality_fields
        )
        for instrument_id, instrument_bars in grouped.items()
    }


def write_native_bundle(
    root: Path,
    *,
    calendar: Sequence[date],
    features: Iterable[tuple[str, pd.DataFrame]],
) -> BundleContents:
    """Write the small, stable subset of Qlib's native day provider format.

    `features` is consumed as a stream: each security is written and dropped
    before the next arrives, so the caller never has to hold the whole snapshot.
    """
    if not calendar:
        raise ValueError("calendar must not be empty")
    root = Path(root)
    calendars_dir = root / "calendars"
    instruments_dir = root / "instruments"
    feature_dir = root / "features"
    calendars_dir.mkdir(parents=True, exist_ok=True)
    instruments_dir.mkdir(parents=True, exist_ok=True)
    feature_dir.mkdir(parents=True, exist_ok=True)

    (calendars_dir / "day.txt").write_text(
        "".join(f"{day.isoformat()}\n" for day in calendar),
        encoding="utf-8",
    )

    instrument_lines: list[str] = []
    written: list[str] = []
    fields: tuple[str, ...] = ()
    calendar_index = pd.Index(calendar)
    for instrument_id, frame in features:
        aligned = frame.reindex(calendar_index)
        present = aligned.notna().any(axis=1)
        if not bool(present.any()):
            continue
        active_dates = aligned.index[present]
        instrument_lines.append(
            f"{instrument_id}\t{active_dates[0].isoformat()}\t{active_dates[-1].isoformat()}\n"
        )
        written.append(instrument_id)
        fields = fields or tuple(aligned.columns)
        instrument_dir = feature_dir / instrument_id.lower()
        instrument_dir.mkdir(parents=True, exist_ok=True)
        for field in aligned.columns:
            values = aligned[field].to_numpy(dtype="<f4", na_value=np.nan)
            payload = np.concatenate((np.array([0], dtype="<f4"), values))
            payload.tofile(instrument_dir / f"{field.lower()}.day.bin")

    # Sorted here rather than assumed of the stream: the caller's ordering is
    # its own business, and all.txt staying deterministic is what keeps a
    # rebuild of the same snapshot byte-identical.
    (instruments_dir / "all.txt").write_text("".join(sorted(instrument_lines)), encoding="utf-8")
    return BundleContents(instruments=tuple(sorted(written)), fields=tuple(sorted(fields)))
