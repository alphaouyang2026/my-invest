from collections.abc import Callable

import httpx

from app.integrations.jquants import JQuantsAdapter, RetryPolicy


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def adapter_for(
    handler: Callable[[httpx.Request], httpx.Response], clock: FakeClock
) -> JQuantsAdapter:
    client = httpx.Client(
        base_url="https://api.jquants.com/v2",
        transport=httpx.MockTransport(handler),
    )
    return JQuantsAdapter(
        api_key="test-secret",
        client=client,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        jitter=lambda: 0.0,
        retry_policy=RetryPolicy(min_interval_seconds=12.5, max_attempts=5),
    )


def test_calendar_returns_every_paginated_row_without_exposing_auth() -> None:
    clock = FakeClock()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.params.get("pagination_key") == "next-page":
            return httpx.Response(200, json={"data": [{"Date": "2026-05-02", "HolDiv": "0"}]})
        return httpx.Response(
            200,
            json={
                "data": [{"Date": "2026-05-01", "HolDiv": "1"}],
                "pagination_key": "next-page",
            },
        )

    adapter = adapter_for(handler, clock)

    result = adapter.fetch_calendar()

    assert result.rows == [
        {"Date": "2026-05-01", "HolDiv": "1"},
        {"Date": "2026-05-02", "HolDiv": "0"},
    ]
    assert result.page_count == 2
    assert requests[1].url.params["pagination_key"] == "next-page"
    assert all(request.headers["x-api-key"] == "test-secret" for request in requests)


def test_429_retry_after_is_honoured_before_returning_daily_bars() -> None:
    clock = FakeClock()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "20"}, json={"message": "slow down"})
        return httpx.Response(
            200,
            json={"data": [{"Date": "2026-05-01", "Code": "72030", "C": 2500}]},
        )

    adapter = adapter_for(handler, clock)

    result = adapter.fetch_daily_bars("2026-05-01")

    assert result.rows[0]["Code"] == "72030"
    assert attempts == 2
    assert clock.sleeps == [20.0]
