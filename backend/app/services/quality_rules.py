"""Row-local data-quality rules.

These rules are pure functions of one bar's own values, which is why they are
evaluated when the immutable `BarVersion` is inserted rather than in a later
pass. That placement is not incidental: a revision that reverts to earlier
content reuses the original version row (A -> B -> A), so a verdict stored
there must be a property of the *content*, not of when it was observed.
Anything needing a neighbouring date, the calendar or a peer group cannot
satisfy that and belongs to the contextual pass instead.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import Decimal

from app.models.market_data import BarQualityStatus

__all__ = [
    "BarQualityStatus",
    "QualityPolicy",
    "QualityRule",
    "evaluate_row_local",
]


class QualityRule(str, enum.Enum):
    MISSING_CRITICAL_FIELD = "missing_critical_field"
    MISSING_OPTIONAL_FIELD = "missing_optional_field"
    NEGATIVE_PRICE = "negative_price"
    NEGATIVE_VOLUME = "negative_volume"
    OHLC_OUT_OF_ORDER = "ohlc_out_of_order"


PRICE_FIELDS = ("raw_open", "raw_high", "raw_low", "raw_close")
VOLUME_FIELDS = ("raw_volume", "adjusted_volume")


#: Close drives valuation and the momentum signal. Without it there is no
#: honest price for the security that day, so the bar is untradable rather
#: than merely incomplete.
CRITICAL_FIELDS = frozenset({"raw_close", "adjusted_close"})

#: Gaps here stop the security entering a pool but leave the close it does
#: have trustworthy.
OPTIONAL_FIELDS = frozenset(
    {
        "raw_open",
        "raw_high",
        "raw_low",
        "raw_volume",
        "trading_value",
        "adjusted_open",
        "adjusted_high",
        "adjusted_low",
        "adjusted_volume",
        "adjustment_factor",
    }
)

#: Worst wins: one column has to answer "can this be traded", and the rule
#: codes are what keep the individual reasons recoverable.
_SEVERITY = {
    BarQualityStatus.OK: 0,
    BarQualityStatus.EXCLUDED: 1,
    BarQualityStatus.UNTRADABLE: 2,
}


@dataclass(frozen=True)
class QualityPolicy:
    """Tunable inputs to the quality rules.

    A dataclass rather than `Settings` so tests can vary a threshold without
    environment variables or a restart, matching how `SyncPolicy` already
    carries the sync knobs.
    """

    critical_fields: frozenset[str] = field(default=CRITICAL_FIELDS)
    optional_fields: frozenset[str] = field(default=OPTIONAL_FIELDS)


def evaluate_row_local(
    values: dict[str, Decimal | None], policy: QualityPolicy
) -> tuple[BarQualityStatus, list[str]]:
    """Return the verdict for one bar and every rule that fired."""
    fired: list[tuple[QualityRule, BarQualityStatus]] = []

    if any(values.get(name) is None for name in policy.critical_fields):
        fired.append((QualityRule.MISSING_CRITICAL_FIELD, BarQualityStatus.UNTRADABLE))
    if any(values.get(name) is None for name in policy.optional_fields):
        fired.append((QualityRule.MISSING_OPTIONAL_FIELD, BarQualityStatus.EXCLUDED))

    if _any_negative(values, PRICE_FIELDS):
        fired.append((QualityRule.NEGATIVE_PRICE, BarQualityStatus.UNTRADABLE))
    # Zero is left alone deliberately: a day with no trades is a legitimate
    # market state. Whether it indicates a suspension needs the calendar, so
    # that question belongs to the contextual pass.
    if _any_negative(values, VOLUME_FIELDS):
        fired.append((QualityRule.NEGATIVE_VOLUME, BarQualityStatus.UNTRADABLE))

    if _ohlc_out_of_order(values):
        fired.append((QualityRule.OHLC_OUT_OF_ORDER, BarQualityStatus.UNTRADABLE))

    status = max(
        (verdict for _, verdict in fired),
        key=_SEVERITY.__getitem__,
        default=BarQualityStatus.OK,
    )
    return status, [rule.value for rule, _ in fired]


def _any_negative(values: dict[str, Decimal | None], names: tuple[str, ...]) -> bool:
    return any(values.get(name) is not None and values[name] < 0 for name in names)


def _ohlc_out_of_order(values: dict[str, Decimal | None]) -> bool:
    """Whether the day's four prices contradict each other.

    Only judged when every bound involved is present: a missing high is the
    missing-field rule's business, and comparing against it here would report
    a second, phantom defect for one cause.
    """
    low, high = values.get("raw_low"), values.get("raw_high")
    if low is None or high is None:
        return False
    if low > high:
        return True
    return any(
        values.get(name) is not None and not (low <= values[name] <= high)
        for name in ("raw_open", "raw_close")
    )
