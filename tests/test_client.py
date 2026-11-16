import asyncio
import dataclasses

import httpx
import pytest

from rated_mcp.client import RatedAPIError, RatedClient
from rated_mcp.config import Settings

SETTINGS = dataclasses.replace(Settings.from_env(), min_request_interval=0, max_retry_wait=0.01)


def make_client(handler) -> RatedClient:
    client = RatedClient("test-key", SETTINGS)
    client._http = httpx.AsyncClient(base_url="https://api.rated.network", transport=httpx.MockTransport(handler))
    return client


def test_retries_429_then_succeeds():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"detail": "Too many requests"})
        return httpx.Response(200, headers={"x-request-id": "abc", "x-rated-compute-units": "7"}, json={"ok": 1})

    result = asyncio.run(make_client(handler).request("get", "/v1/eth"))
    assert len(calls) == 3
    assert result.data == {"ok": 1}
    assert result.request_id == "abc"
    assert result.compute_units == "7"


def test_gives_up_after_max_retries():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"x-request-id": "rid-429"}, json={"detail": "Too many requests"})

    with pytest.raises(RatedAPIError) as exc:
        asyncio.run(make_client(handler).request("get", "/v1/eth"))
    assert len(calls) == SETTINGS.max_retries + 1
    assert "rid-429" in str(exc.value)


def test_errors_include_request_id_and_writes_are_not_retried_on_5xx():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, headers={"x-request-id": "rid-503"}, json={"detail": "Internal server error."})

    with pytest.raises(RatedAPIError) as exc:
        asyncio.run(make_client(handler).request("post", "/v1/tags", json_body={"name": "x"}))
    assert len(calls) == 1
    assert exc.value.status == 503
    assert "request ID: rid-503" in str(exc.value)
