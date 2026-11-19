"""Shared types and polite-HTTP plumbing for fare sources.

Every source must:
  * identify itself with a descriptive User-Agent,
  * respect a minimum interval between requests (client-side rate limit),
  * back off exponentially on 429/5xx and honour Retry-After,
  * never put credentials into anything that gets stored or logged.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx

log = logging.getLogger(__name__)

# httpx logs full request URLs at INFO, and SerpApi's key is a query parameter. Silence it here,
# where the HTTP client is defined, so this holds no matter how logging is configured elsewhere.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

# Query/body parameter names that carry credentials. Stripped before anything is persisted.
SECRET_PARAM_NAMES = frozenset({"api_key", "token", "apikey", "access_token", "key"})

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class SourceName(StrEnum):
    TRAVELPAYOUTS = "travelpayouts"
    SERPAPI = "serpapi"


class SourceError(RuntimeError):
    """A source returned an unusable response (after retries)."""


class QuotaExceededError(RuntimeError):
    """The monthly API budget for a source is spent."""


@dataclass(frozen=True)
class RawResponse:
    """Exactly what a source returned, plus the (credential-free) request that produced it."""

    source: SourceName
    endpoint: str
    request_params: dict[str, Any]
    http_status: int
    payload: dict[str, Any]
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        canonical = json.dumps(self.payload, sort_keys=True, separators=(",", ":"))
        object.__setattr__(self, "sha256", hashlib.sha256(canonical.encode()).hexdigest())


def sanitize_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``params`` with credential-bearing keys removed."""
    return {k: v for k, v in params.items() if k.lower() not in SECRET_PARAM_NAMES}


class RateLimiter:
    """Enforces a minimum interval between calls (simple, deterministic, testable)."""

    def __init__(
        self,
        min_interval_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._min_interval = min_interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None:
            remaining = self._min_interval - (now - self._last)
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last = now


class PoliteClient:
    """httpx wrapper adding rate limiting, retries with backoff, and JSON validation."""

    def __init__(
        self,
        *,
        source: SourceName,
        base_url: str,
        user_agent: str,
        timeout_seconds: float,
        min_interval_seconds: float,
        max_attempts: int = 3,
        default_headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("sources must be reached over HTTPS")
        self.source = source
        self._sleep = sleep
        self._max_attempts = max_attempts
        self._limiter = RateLimiter(min_interval_seconds, sleep=sleep)
        headers = {"User-Agent": user_agent, "Accept": "application/json"}
        headers.update(default_headers or {})
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_json(self, endpoint: str, params: Mapping[str, Any]) -> RawResponse:
        last_error: str = "no attempts made"
        for attempt in range(1, self._max_attempts + 1):
            self._limiter.wait()
            try:
                response = self._client.get(endpoint, params=dict(params))
            except httpx.TransportError as exc:
                # Don't log str(exc): some transport errors include the full URL (and key).
                last_error = f"transport error: {type(exc).__name__}"
                self._backoff(attempt, None)
                continue

            if response.status_code in RETRYABLE_STATUS:
                last_error = f"HTTP {response.status_code}"
                log.warning(
                    "%s %s returned %s (attempt %d)",
                    self.source,
                    endpoint,
                    response.status_code,
                    attempt,
                )
                self._backoff(attempt, response.headers.get("Retry-After"))
                continue
            if response.status_code >= 400:
                raise SourceError(f"{self.source} {endpoint}: HTTP {response.status_code}")

            try:
                payload = response.json()
            except ValueError as exc:
                raise SourceError(f"{self.source} {endpoint}: response was not JSON") from exc
            if not isinstance(payload, dict):
                raise SourceError(f"{self.source} {endpoint}: expected a JSON object")
            return RawResponse(
                source=self.source,
                endpoint=endpoint,
                request_params=sanitize_params(params),
                http_status=response.status_code,
                payload=payload,
            )
        raise SourceError(f"{self.source} {endpoint}: gave up after retries ({last_error})")

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        if attempt >= self._max_attempts:
            return
        delay = 2.0**attempt + random.uniform(0, 1)  # noqa: S311 - jitter, not crypto
        if retry_after and retry_after.isdigit():
            delay = max(delay, min(float(retry_after), 300.0))
        self._sleep(delay)
