from datetime import date
from uuid import UUID

import pytest

from app.research.definition import ResearchDefinition


SNAPSHOT_ID = UUID("11111111-1111-1111-1111-111111111111")


def definition(**overrides) -> ResearchDefinition:
    values = {
        "data_snapshot_id": SNAPSHOT_ID,
        "observation_start": date(2025, 1, 6),
        "observation_end": date(2025, 3, 31),
        "lookback_days": 126,
        "skip_days": 21,
    }
    values.update(overrides)
    return ResearchDefinition(**values)


def test_equal_research_semantics_have_one_canonical_identity() -> None:
    first = definition()
    second = definition()

    assert first.fingerprint == second.fingerprint
    assert first.canonical_payload == second.canonical_payload


def test_a_parameter_change_creates_a_different_research_identity() -> None:
    assert definition().fingerprint != definition(skip_days=20).fingerprint


def test_invalid_observation_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="observation_start"):
        definition(
            observation_start=date(2025, 4, 1),
            observation_end=date(2025, 3, 31),
        )
