from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx


class JQuantsError(RuntimeError):
    """A sanitised J-Quants failure safe to persist and show in the UI."""


@dataclass(frozen=True)
class RetryPolicy:
    min_interval_seconds: float = 15.0
    max_attempts: int = 5
    base_backoff_seconds: float = 60.0
    max_backoff_seconds: float = 60.0


@dataclass(frozen=True)
class FetchResult:
    rows: list[dict[str, Any]]
    pages: list[dict[str, Any]]

    @property
    def page_count(self) -> int:
        return len(self.pages)


class JQuantsAdapter:
    API_VERSION = "v2"
    ADAPTER_VERSION = "1"

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        retry_policy: RetryPolicy = RetryPolicy(),
    ) -> None:
        if not api_key.strip():
            raise ValueError("J-Quants API key is empty")
        self._client = client or httpx.Client(
            base_url="https://api.jquants.com/v2",
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0),
        )
        self._headers = {"x-api-key": api_key}
        self._monotonic = monotonic
        self._sleep = sleep
        self._jitter = jitter
        self._policy = retry_policy
        self._last_request_at: float | None = None

    def fetch_calendar(self) -> FetchResult:
        return self._fetch_all("/markets/calendar", {})

    def fetch_daily_bars(self, trade_date: str) -> FetchResult:
        if not trade_date:
            raise ValueError("trade_date is required")
        return self._fetch_all("/equities/bars/daily", {"date": trade_date})

    def fetch_master(self, as_of_date: str) -> FetchResult:
        if not as_of_date:
            raise ValueError("as_of_date is required")
        return self._fetch_all("/equities/master", {"date": as_of_date})

    def _fetch_all(self, path: str, base_params: dict[str, str]) -> FetchResult:
        rows: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        pagination_key: str | None = None
        while True:
            params = dict(base_params)
            if pagination_key:
                params["pagination_key"] = pagination_key
            payload = self._request(path, params)
            page_rows = payload.get("data")
            if not isinstance(page_rows, list):
                raise JQuantsError(f"J-Quants {path} response is missing a data array")
            pages.append(payload)
            rows.extend(page_rows)
            pagination_key = payload.get("pagination_key")
            if not pagination_key:
                return FetchResult(rows=rows, pages=pages)

    def _request(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        for attempt in range(1, self._policy.max_attempts + 1):
            self._wait_for_rate_limit()
            try:
                response = self._client.get(path, params=params, headers=self._headers)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt == self._policy.max_attempts:
                    raise JQuantsError(f"J-Quants {path} failed after retries") from exc
                self._sleep(self._backoff(attempt))
                continue

            if response.status_code < 400:
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise JQuantsError(f"J-Quants {path} returned invalid JSON") from exc
                if not isinstance(payload, dict):
                    raise JQuantsError(f"J-Quants {path} returned an invalid response object")
                return payload

            retryable = response.status_code in {408, 429} or response.status_code >= 500
            if not retryable or attempt == self._policy.max_attempts:
                raise JQuantsError(f"J-Quants {path} returned HTTP {response.status_code}")
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else self._backoff(attempt)
            self._sleep(delay)

        raise AssertionError("retry loop exhausted")

    def _wait_for_rate_limit(self) -> None:
        now = self._monotonic()
        if self._last_request_at is not None:
            remaining = self._policy.min_interval_seconds - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request_at = self._monotonic()

    def _backoff(self, attempt: int) -> float:
        base = min(
            self._policy.max_backoff_seconds,
            self._policy.base_backoff_seconds * (2 ** (attempt - 1)),
        )
        return base + self._jitter()
