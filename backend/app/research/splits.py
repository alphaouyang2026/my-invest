"""Where train stops, where valid starts, and the cross-section thrown away between them.

A weekly label does not end in the week it starts. For a cross-section at `W[k]`
the position is entered on the session after `W[k]` and closed on the session
after `W[k+1]`, so the label is only known roughly `Wk + 1 week + 1 day`. Leave a
one-week gap between segments and the last training label still resolves inside
the validation period — the model was fitted on a number that could not be known
when validation began.

Closing that means dropping a whole cross-section, not widening a gap:

    W1  train's last cross-section
    W2  embargo, belongs to no segment
    W3  valid's first cross-section        label_exit(W1) < W3  ✓

Everything here works in sessions of the exchange calendar. "Add seven days" is
wrong on a holiday week, where consecutive weekly cross-sections can sit three
sessions apart instead of five.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date


class SplitError(ValueError):
    """The requested split cannot be honoured.

    Surfaces as a 400: silently repairing a split would leave the caller
    believing they trained on a range they did not.
    """


@dataclass(frozen=True)
class Segment:
    name: str
    start: date
    end: date
    observations: tuple[date, ...]

    def as_payload(self) -> dict:
        """The part of a segment that belongs to the experiment's identity.

        The observation dates and their count are deliberately absent: they are
        derived from the boundaries plus the snapshot's trading calendar, so
        including them would make the definition carry a fact it does not
        choose. It would also make the definition impossible to rebuild from
        storage without re-reading the calendar, which is exactly what
        `compile_execution_spec` must be able to do before touching any data.
        """
        return {
            "name": self.name,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }


@dataclass(frozen=True)
class SplitPlan:
    """Three segments plus the cross-sections deliberately excluded."""

    train: Segment
    valid: Segment
    test: Segment
    embargo: tuple[date, ...]

    @property
    def segments(self) -> tuple[Segment, ...]:
        return (self.train, self.valid, self.test)

    def as_payload(self) -> dict:
        return {
            "train": self.train.as_payload(),
            "valid": self.valid.as_payload(),
            "test": self.test.as_payload(),
            "embargo": [item.isoformat() for item in self.embargo],
        }


def label_exit_session(
    observation: date,
    *,
    weekly_observations: Sequence[date],
    calendar: Sequence[date],
) -> date | None:
    """The session whose open closes the position opened after `observation`.

    None when the holding period runs past what the snapshot covers — the label
    has not matured, which is a fact about the data rather than an error.
    """
    try:
        index = weekly_observations.index(observation)
    except ValueError as exc:
        raise SplitError(f"{observation} is not a weekly cross-section") from exc
    if index + 1 >= len(weekly_observations):
        return None
    try:
        exit_anchor = calendar.index(weekly_observations[index + 1])
    except ValueError as exc:
        raise SplitError(f"{weekly_observations[index + 1]} is not a trading session") from exc
    if exit_anchor + 1 >= len(calendar):
        return None
    return calendar[exit_anchor + 1]


def build_split_plan(
    *,
    weekly_observations: Sequence[date],
    calendar: Sequence[date],
    train_end: date,
    valid_start: date,
    valid_end: date,
    test_start: date,
    test_end: date,
    train_start: date | None = None,
) -> SplitPlan:
    """Assemble and validate the three segments.

    Validation is deliberately in terms of the two label/feature inequalities
    rather than a gap width: the inequalities are what leakage actually depends
    on, and a width can satisfy the letter while violating them (see module
    docstring).
    """
    observations = list(weekly_observations)
    if not observations:
        raise SplitError("No weekly cross-sections available in the requested range")
    train_start = train_start or observations[0]

    ordered = [train_start, train_end, valid_start, valid_end, test_start, test_end]
    if any(earlier > later for earlier, later in zip(ordered, ordered[1:])):
        raise SplitError(
            "Segments must be strictly ordered: "
            "train_start <= train_end < valid_start <= valid_end < test_start <= test_end"
        )
    if train_end >= valid_start or valid_end >= test_start:
        raise SplitError("Segments must not overlap or touch; each boundary needs an embargo")

    def window(start: date, end: date) -> tuple[date, ...]:
        return tuple(item for item in observations if start <= item <= end)

    segments = (
        Segment("train", train_start, train_end, window(train_start, train_end)),
        Segment("valid", valid_start, valid_end, window(valid_start, valid_end)),
        Segment("test", test_start, test_end, window(test_start, test_end)),
    )
    for segment in segments:
        if not segment.observations:
            raise SplitError(f"Segment {segment.name!r} contains no weekly cross-section")

    train, valid, test = segments
    _assert_isolated(train, valid, observations=observations, calendar=calendar)
    _assert_isolated(valid, test, observations=observations, calendar=calendar)

    claimed = {item for segment in segments for item in segment.observations}
    embargo = tuple(
        item
        for item in observations
        if train.start <= item <= test.end and item not in claimed
    )
    return SplitPlan(train=train, valid=valid, test=test, embargo=embargo)


def _assert_isolated(
    earlier: Segment,
    later: Segment,
    *,
    observations: Sequence[date],
    calendar: Sequence[date],
) -> None:
    """`label_exit(earlier's last) < feature_cutoff(later's first)`.

    The feature cutoff is the close of the cross-section day itself, so the
    comparison is against that date: an exit on the same day would mean the
    label resolved on the very session whose features the next segment reads.
    """
    last = earlier.observations[-1]
    first = later.observations[0]
    exit_session = label_exit_session(last, weekly_observations=observations, calendar=calendar)
    if exit_session is None:
        raise SplitError(
            f"Segment {earlier.name!r} ends at {last}, whose label never matures within the snapshot"
        )
    if exit_session >= first:
        raise SplitError(
            f"Label leakage across the {earlier.name}/{later.name} seam: the label of {last} "
            f"resolves on {exit_session}, which is not before {later.name}'s first feature "
            f"cutoff {first}. Exclude at least one further weekly cross-section."
        )


def propose_split(
    weekly_observations: Sequence[date],
    *,
    train_ratio: float = 0.6,
    valid_ratio: float = 0.2,
) -> dict[str, date]:
    """Default boundaries for the creation form.

    The two embargo cross-sections are removed **before** the ratios are
    applied. Splitting first and squeezing the embargo out afterwards silently
    shortens whichever segment happens to lose the round, which is how a "60/20/20"
    label ends up describing something else.
    """
    observations = list(weekly_observations)
    budget = len(observations) - 2
    if budget < 3:
        raise SplitError(
            f"At least 5 weekly cross-sections are needed for a split with two embargoes, "
            f"got {len(observations)}"
        )
    train_count = max(1, round(budget * train_ratio))
    valid_count = max(1, round(budget * valid_ratio))
    if train_count + valid_count >= budget:
        train_count = max(1, budget - 2)
        valid_count = 1
    test_count = budget - train_count - valid_count

    train = observations[:train_count]
    valid = observations[train_count + 1 : train_count + 1 + valid_count]
    test = observations[train_count + valid_count + 2 :][:test_count]
    return {
        "train_start": train[0],
        "train_end": train[-1],
        "valid_start": valid[0],
        "valid_end": valid[-1],
        "test_start": test[0],
        "test_end": test[-1],
    }
