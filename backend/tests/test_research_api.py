from fastapi.testclient import TestClient

from app.api.research import get_research_application
from app.main import app


class FakeResearchApplication:
    def __init__(self) -> None:
        self.requests = []

    def create_run(self, request):
        self.requests.append(request)
        return {
            "id": "22222222-2222-2222-2222-222222222222",
            "experiment_id": "33333333-3333-3333-3333-333333333333",
            "status": "queued",
            "processed_dates": 0,
            "total_dates": 0,
            "warnings": [],
        }

    def get_ranked_scores(self, run_id, observation_date):
        return {
            "run_id": str(run_id),
            "observation_date": observation_date,
            "scores": [],
            "exclusions": [
                {
                    "observation_date": "2025-03-31",
                    "instrument_id": "44444444-4444-4444-4444-444444444444",
                    "reason": "insufficient_turnover",
                }
            ],
        }


def test_user_can_create_a_registered_momentum_research_run() -> None:
    research = FakeResearchApplication()
    app.dependency_overrides[get_research_application] = lambda: research
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/research/runs",
                json={
                    "data_snapshot_id": "11111111-1111-1111-1111-111111111111",
                    "observation_start": "2025-01-06",
                    "observation_end": "2025-03-31",
                    "lookback_days": 126,
                    "skip_days": 21,
                },
            )
    finally:
        app.dependency_overrides.pop(get_research_application, None)

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert research.requests[0].lookback_days == 126


def test_ranked_scores_publish_universe_exclusions() -> None:
    research = FakeResearchApplication()
    app.dependency_overrides[get_research_application] = lambda: research
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/research/runs/22222222-2222-2222-2222-222222222222/"
                "ranked-scores?observation_date=2025-03-31"
            )
    finally:
        app.dependency_overrides.pop(get_research_application, None)

    assert response.status_code == 200
    assert response.json()["exclusions"][0]["reason"] == "insufficient_turnover"
