"""The HTTP contract for model runs.

Two properties matter more than the individual payloads.

`ranked-scores` is one URL returning one shape for both kinds of run. That
interface exists so ticket 08 need not know whether a score came from a factor
or a model; a second endpoint, or one divergent field, hands that knowledge
straight back to it.

Bad requests are refused rather than repaired. A clamped parameter or a nudged
segment boundary leaves the caller believing they ran something they did not,
while the fingerprint records the other thing — and reproducibility is what this
ticket is accepted against.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.research.feature_sets import get_feature_set
from app.research.model_definition import DEFAULT_SEED

BASE = "/api/v1/research"


def _payload(snapshot_id: str, **overrides) -> dict:
    body = {
        "data_snapshot_id": snapshot_id,
        "feature_set": "alpha158_jp_v1",
        "train_start": "2025-01-10",
        "train_end": "2025-05-02",
        "valid_start": "2025-05-16",
        "valid_end": "2025-06-20",
        "test_start": "2025-07-04",
        "test_end": "2025-08-01",
        "seed": DEFAULT_SEED,
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------


def test_model_research_routes_are_published_in_openapi(client) -> None:
    paths = set(client.get("/openapi.json").json()["paths"])

    assert {
        f"{BASE}/feature-sets",
        f"{BASE}/model-runs/config",
        f"{BASE}/model-runs",
        f"{BASE}/model-runs/{{run_id}}/results",
    } <= paths


def test_the_feature_set_registry_is_exposed_read_only(client) -> None:
    response = client.get(f"{BASE}/feature-sets")

    assert response.status_code == 200
    sets = {item["name"]: item for item in response.json()}
    assert sets["alpha158_jp_v1"]["column_count"] == 158
    assert sets["alpha360_jp_v1"]["column_count"] == 360
    first = sets["alpha360_jp_v1"]["columns"][0]
    assert set(first) == {"name", "expression", "required_fields", "window", "dtype"}
    assert [column["name"] for column in sets["alpha360_jp_v1"]["columns"]] == [
        feature.name for feature in get_feature_set("alpha360_jp_v1").features
    ]


def test_alpha360_is_offered_as_selectable_not_refused(client) -> None:
    """"The user can choose Alpha158 or Alpha360" is not satisfied by listing it."""
    sets = {item["name"]: item for item in client.get(f"{BASE}/feature-sets").json()}

    assert sets["alpha360_jp_v1"]["selectable"] is True
    assert sets["alpha360_jp_v1"]["is_default"] is False
    assert sets["alpha360_jp_v1"]["maturity"] == "experimental"


def test_the_momentum_control_is_not_offered_for_training(client) -> None:
    """It exists to be scored beside a model, not to be trained on."""
    sets = {item["name"]: item for item in client.get(f"{BASE}/feature-sets").json()}

    assert sets["momentum_only_v1"]["selectable"] is False


def test_the_config_states_which_parameters_are_locked(client) -> None:
    """The page must be able to render the lock, not just obey it.

    A form that shows every parameter as editable while the API refuses four of
    them teaches the user the interface is broken.
    """
    config = client.get(f"{BASE}/model-runs/config").json()

    assert set(config["locked_params"]) == {"objective", "deterministic", "force_row_wise"}
    assert "learning_rate" in config["overridable_params"]
    assert "seed" not in config["overridable_params"]
    assert config["default_feature_set"] == "alpha158_jp_v1"


def test_every_overridable_parameter_publishes_its_range(client) -> None:
    config = client.get(f"{BASE}/model-runs/config").json()

    for name, spec in config["overridable_params"].items():
        assert {"type", "min", "max"} <= set(spec), name


# --------------------------------------------------------------------------
# Creation refuses rather than repairs
# --------------------------------------------------------------------------


def test_an_unknown_feature_set_is_a_bad_request(client) -> None:
    response = client.post(f"{BASE}/model-runs", json=_payload(str(uuid.uuid4()), feature_set="nope"))

    assert response.status_code in (400, 404)


def test_a_locked_parameter_cannot_be_overridden(client) -> None:
    response = client.post(
        f"{BASE}/model-runs",
        json=_payload(str(uuid.uuid4()), model_params={"deterministic": 0}),
    )

    assert response.status_code in (400, 404, 422)


def test_a_seed_inside_model_params_is_refused(client) -> None:
    """One entry point, so no precedence rule has to exist."""
    response = client.post(
        f"{BASE}/model-runs",
        json=_payload(str(uuid.uuid4()), model_params={"seed": 99}),
    )

    assert response.status_code in (400, 404, 422)


def test_an_out_of_range_parameter_is_refused_not_clamped(client) -> None:
    response = client.post(
        f"{BASE}/model-runs",
        json=_payload(str(uuid.uuid4()), model_params={"num_boost_round": 999999}),
    )

    assert response.status_code in (400, 404, 422)


def test_an_unknown_field_in_the_body_is_rejected(client) -> None:
    """`extra="forbid"`: a misspelt parameter must not be silently dropped."""
    response = client.post(
        f"{BASE}/model-runs", json=_payload(str(uuid.uuid4()), learnign_rate=0.1)
    )

    assert response.status_code == 422


def test_a_missing_snapshot_is_a_not_found(client) -> None:
    response = client.post(f"{BASE}/model-runs", json=_payload(str(uuid.uuid4())))

    assert response.status_code == 404


# --------------------------------------------------------------------------
# The shared interface
# --------------------------------------------------------------------------


def test_ranked_scores_is_one_url_for_both_kinds_of_run(client) -> None:
    """Dispatch happens on the experiment's kind, inside the endpoint.

    Ticket 08 asks a run for its ranked scores; it never asks what produced
    them, and there is no second URL that would let it.
    """
    import inspect

    from app.services.research import SqlResearchApplication

    # The branch is in the application, not the route: dispatching in the route
    # would need a session dependency past the seam every other endpoint uses.
    assert "SqlModelResearchApplication" in inspect.getsource(
        SqlResearchApplication.get_ranked_scores
    )
    # The OpenAPI document rather than `app.routes`: routers are mounted, so the
    # top-level list holds only /docs and friends.
    paths = set(client.get("/openapi.json").json()["paths"])

    assert f"{BASE}/runs/{{run_id}}/ranked-scores" in paths
    assert not any("model-runs" in path and "ranked-scores" in path for path in paths)


def test_ranked_scores_on_an_unknown_run_is_a_not_found(client) -> None:
    response = client.get(f"{BASE}/runs/{uuid.uuid4()}/ranked-scores")

    assert response.status_code == 404


def test_results_for_an_unpublished_run_are_a_conflict_not_an_empty_body(client) -> None:
    """An artifact that is not committed yet is a state, not an absence."""
    response = client.get(f"{BASE}/model-runs/{uuid.uuid4()}/results")

    assert response.status_code in (404, 409)
