"""Thin async HTTP client for the Rated API with throttling and 429 backoff."""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from rated_mcp.config import BASE_URL, Settings

RETRYABLE_SERVER_ERRORS = {502, 503, 504}


class RatedAPIError(Exception):
    """An error response from the Rated API, with a message safe to show to users."""

    def __init__(self, status: int, message: str, request_id: str | None):
        self.status = status
        self.request_id = request_id
        suffix = f" (request ID: {request_id})" if request_id else " (no request ID returned)"
        super().__init__(f"Rated API returned HTTP {status}: {message}{suffix}")


@dataclass
class RatedResponse:
    status: int
    data: Any
    request_id: str | None
    compute_units: str | None


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500] or response.reason_phrase
    detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
    if isinstance(detail, str):
        return detail
    return json.dumps(detail)[:1500]


def _hint(status: int) -> str:
    return {
        401: " Check that your Rated API key is valid and not expired (console.rated.network).",
        403: " Your API key's plan may not include this endpoint.",
        404: " Double-check the identifier you passed.",
        429: " Rate limit still exceeded after retries; wait a bit and try again.",
    }.get(status, "")


class RatedClient:
    def __init__(self, api_key: str, settings: Settings):
        self._settings = settings
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=settings.timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "rated-mcp/0.1",
            },
        )
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._last_request + self._settings.min_request_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after")
        try:
            delay = float(retry_after) if retry_after is not None else 2.0**attempt
        except ValueError:
            delay = 2.0**attempt
        return min(delay + random.uniform(0, 0.5), self._settings.max_retry_wait)

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> RatedResponse:
        attempt = 0
        while True:
            await self._throttle()
            response = await self._http.request(
                method.upper(), url, params=params, headers=headers, json=json_body
            )
            status = response.status_code
            # 429 means the request was not processed, so retrying is safe for any method.
            # Server errors are only retried for reads.
            retryable = status == 429 or (method.lower() == "get" and status in RETRYABLE_SERVER_ERRORS)
            if retryable and attempt < self._settings.max_retries:
                await asyncio.sleep(self._retry_delay(response, attempt))
                attempt += 1
                continue
            break

        request_id = response.headers.get("x-request-id")
        if status >= 400:
            raise RatedAPIError(status, _error_detail(response) + _hint(status), request_id)
        try:
            data = response.json() if response.content else None
        except ValueError:
            data = response.text
        return RatedResponse(
            status=status,
            data=data,
            request_id=request_id,
            compute_units=response.headers.get("x-rated-compute-units"),
        )


def validate_next_url(url: str) -> str:
    """Only allow following pagination links back to the Rated API."""
    parts = urlsplit(url.strip())
    if parts.hostname != urlsplit(BASE_URL).hostname:
        raise ValueError(f"Refusing to fetch {parts.hostname!r}: only api.rated.network links are allowed.")
    if not parts.path.startswith(("/v0/", "/v1/")):
        raise ValueError("Pagination links must point to a /v0/ or /v1/ Rated API path.")
    # The API sometimes returns http:// links; always use HTTPS.
    return parts._replace(scheme="https").geturl()
