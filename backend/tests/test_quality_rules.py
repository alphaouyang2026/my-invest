"""Row-local quality rules: pure functions of one bar's own values.

These are the rules that can be settled without looking at any other row, so
they are computed once when the immutable BarVersion is inserted. Anything
needing a neighbour, a calendar or a peer group belongs to the contextual pass
instead.
"""

from __future__ import annotations

from decimal import Decimal

from app.services.quality_rules import (
    BarQualityStatus,
    QualityPolicy,
    QualityRule,
    evaluate_row_local,
)

POLICY = QualityPolicy()


def values(**overrides) -> dict[str, Decimal | None]:
    """A healthy bar, with named fields overridden."""
    base = {
        "raw_open": Decimal("100"),
        "raw_high": Decimal("110"),
        "raw_low": Decimal("90"),
        "raw_close": Decimal("105"),
        "raw_volume": Decimal("1000"),
        "trading_value": Decimal("105000"),
        "adjusted_open": Decimal("100"),
        "adjusted_high": Decimal("110"),
        "adjusted_low": Decimal("90"),
        "adjusted_close": Decimal("105"),
        "adjusted_volume": Decimal("1000"),
        "adjustment_factor": Decimal("1"),
    }
    base.update(overrides)
    return base


def test_a_healthy_bar_is_ok_with_no_rule_codes():
    assert evaluate_row_local(values(), POLICY) == (BarQualityStatus.OK, [])


def test_a_missing_closing_price_makes_the_security_untradable():
    """Close drives valuation and the momentum signal; without it there is no
    honest way to trade or price the security that day."""
    status, codes = evaluate_row_local(values(raw_close=None), POLICY)

    assert status is BarQualityStatus.UNTRADABLE
    assert QualityRule.MISSING_CRITICAL_FIELD.value in codes


def test_a_missing_optional_field_only_excludes_the_security():
    """Volume gaps stop the security entering a pool; they do not make the
    close price it still has a lie."""
    status, codes = evaluate_row_local(values(raw_volume=None), POLICY)

    assert status is BarQualityStatus.EXCLUDED
    assert codes == [QualityRule.MISSING_OPTIONAL_FIELD.value]


def test_the_worst_verdict_wins_and_every_code_is_kept():
    """One column has to answer 'can this be traded'; the codes are what keep
    the reason recoverable."""
    status, codes = evaluate_row_local(values(raw_close=None, raw_volume=None), POLICY)

    assert status is BarQualityStatus.UNTRADABLE
    assert sorted(codes) == sorted(
        [QualityRule.MISSING_CRITICAL_FIELD.value, QualityRule.MISSING_OPTIONAL_FIELD.value]
    )


def test_a_negative_price_makes_the_security_untradable():
    status, codes = evaluate_row_local(values(raw_low=Decimal("-1")), POLICY)

    assert status is BarQualityStatus.UNTRADABLE
    assert QualityRule.NEGATIVE_PRICE.value in codes


def test_a_negative_volume_makes_the_security_untradable():
    status, codes = evaluate_row_local(values(raw_volume=Decimal("-5")), POLICY)

    assert status is BarQualityStatus.UNTRADABLE
    assert QualityRule.NEGATIVE_VOLUME.value in codes


def test_zero_volume_is_not_a_defect():
    """A day with no trades is a legitimate market state, not corruption.
    Whether it means a suspension is a calendar question, not a row-local one."""
    assert evaluate_row_local(values(raw_volume=Decimal("0")), POLICY) == (
        BarQualityStatus.OK,
        [],
    )


def test_a_low_above_its_high_makes_the_security_untradable():
    status, codes = evaluate_row_local(
        values(raw_low=Decimal("120"), raw_high=Decimal("110")), POLICY
    )

    assert status is BarQualityStatus.UNTRADABLE
    assert QualityRule.OHLC_OUT_OF_ORDER.value in codes


def test_a_close_outside_the_days_range_makes_the_security_untradable():
    status, codes = evaluate_row_local(values(raw_close=Decimal("999")), POLICY)

    assert status is BarQualityStatus.UNTRADABLE
    assert QualityRule.OHLC_OUT_OF_ORDER.value in codes


def test_ohlc_is_not_judged_when_a_bound_is_missing():
    """Absence is the missing-field rule's business; inventing a comparison
    against None would report a second, phantom defect for one cause."""
    _, codes = evaluate_row_local(values(raw_high=None), POLICY)

    assert QualityRule.OHLC_OUT_OF_ORDER.value not in codes
