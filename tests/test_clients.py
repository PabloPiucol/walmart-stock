import json
from pathlib import Path

import httpx
import pytest

from app.clients import (
    ApiError,
    AuthenticationError,
    RateLimitError,
    WalmartAuthenticator,
    WalmartClient,
)


def response(
    status: int,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    request = httpx.Request("GET", "https://example.test")
    return httpx.Response(status, json=payload, headers=headers, request=request)


def text_response(status: int, body: str) -> httpx.Response:
    request = httpx.Request("GET", "https://example.test")
    return httpx.Response(status, text=body, request=request)


def authenticator() -> WalmartAuthenticator:
    return WalmartAuthenticator(
        "https://example.test",
        "client",
        "secret",
        "partner",
        "channel",
        "cl",
    )


def walmart_client() -> WalmartClient:
    auth = authenticator()
    auth._access_token = "token"
    auth._token_expires_at = float("inf")
    return WalmartClient(auth)


def real_feed_status_payload() -> dict:
    text = Path("walmart_feedId_request.txt").read_text()
    return json.loads(text.split("[RESPONSE]", 1)[1])


def test_access_token_uses_basic_auth_and_is_reused(monkeypatch):
    client = authenticator()
    calls = []

    def fake_post(path, **kwargs):
        calls.append((path, kwargs))
        return response(200, {"access_token": "token", "expires_in": 900})

    monkeypatch.setattr(client.client, "post", fake_post)

    assert client.access_token() == "token"
    assert client.access_token() == "token"
    assert len(calls) == 1
    assert calls[0][0] == "/v3/token"
    assert calls[0][1]["auth"] == ("client", "secret")
    assert calls[0][1]["headers"]["WM_MARKET"] == "cl"
    assert calls[0][1]["headers"]["WM_PARTNER.ID"] == "partner"
    assert calls[0][1]["headers"]["WM_CONSUMER.CHANNEL.TYPE"] == "channel"
    assert calls[0][1]["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert calls[0][1]["data"] == {"grant_type": "client_credentials"}
    assert client.last_token_diagnostic["expires_in"] == 900
    assert client.last_token_diagnostic["correlation_id"]
    assert "access_token" not in client.last_token_diagnostic


def test_authenticator_allows_missing_partner_id_and_omits_header(monkeypatch):
    client = WalmartAuthenticator("https://example.test", "client", "secret")
    calls = []
    monkeypatch.setattr(
        client.client,
        "post",
        lambda path, **kwargs: calls.append((path, kwargs))
        or response(200, {"access_token": "token", "expires_in": 900}),
    )

    assert client.access_token() == "token"
    assert "WM_PARTNER.ID" not in calls[0][1]["headers"]


def test_business_headers_omit_partner_id():
    headers = authenticator().business_headers("token", "correlation")

    assert headers["WM_SEC.ACCESS_TOKEN"] == "token"
    assert headers["WM_QOS.CORRELATION_ID"] == "correlation"
    assert "WM_PARTNER.ID" not in headers


def test_invalid_token_credentials_are_not_retried(monkeypatch):
    client = authenticator()
    calls = 0

    def fake_post(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return response(401, {"error": [{"code": "UNAUTHORIZED"}]})

    monkeypatch.setattr(client.client, "post", fake_post)

    with pytest.raises(AuthenticationError, match="UNAUTHORIZED"):
        client.access_token()

    assert calls == 1


def test_authentication_error_handles_non_json_response(monkeypatch):
    client = authenticator()
    monkeypatch.setattr(
        client.client,
        "post",
        lambda *_args, **_kwargs: text_response(401, "upstream denied"),
    )

    with pytest.raises(AuthenticationError, match="upstream denied"):
        client.access_token()


def test_business_request_401_reauthenticates_and_retries_once(monkeypatch):
    client = walmart_client()
    request_tokens = []
    token_calls = 0

    def fake_post(*_args, **_kwargs):
        nonlocal token_calls
        token_calls += 1
        return response(200, {"access_token": "fresh-token", "expires_in": 900})

    def fake_request(_method, _path, **kwargs):
        request_tokens.append(kwargs["headers"]["WM_SEC.ACCESS_TOKEN"])
        if len(request_tokens) == 1:
            return response(401, {"error": [{"code": "UNAUTHORIZED"}]})
        return response(200, {"feedId": "feed-1"})

    monkeypatch.setattr(client.authenticator.client, "post", fake_post)
    monkeypatch.setattr(client.client, "request", fake_request)

    assert client.request("GET", "/v3/feeds") == {"feedId": "feed-1"}
    assert request_tokens == ["token", "fresh-token"]
    assert token_calls == 1


def test_business_request_does_not_repeat_authentication_after_second_401(monkeypatch):
    client = walmart_client()
    token_calls = 0

    def fake_post(*_args, **_kwargs):
        nonlocal token_calls
        token_calls += 1
        return response(200, {"access_token": "fresh-token", "expires_in": 900})

    monkeypatch.setattr(client.authenticator.client, "post", fake_post)
    monkeypatch.setattr(
        client.client,
        "request",
        lambda *_args, **_kwargs: response(401, {"error": [{"code": "UNAUTHORIZED"}]}),
    )

    with pytest.raises(ApiError, match="HTTP 401"):
        client.request("GET", "/v3/feeds")

    assert token_calls == 1


def test_request_exposes_rate_limit_and_retry_after(monkeypatch):
    client = walmart_client()
    monkeypatch.setattr(
        client.client,
        "request",
        lambda *_args, **_kwargs: response(
            429,
            {"error": [{"code": "REQUEST_THRESHOLD_VIOLATED.GMP_GATEWAY_API"}]},
            {"retry-after": "45"},
        ),
    )

    with pytest.raises(RateLimitError) as error:
        client.request("GET", "/v3/feeds")

    assert error.value.retry_after == 45
    assert "REQUEST_THRESHOLD_VIOLATED" in str(error.value)


def test_inventory_feed_uses_official_chile_shape(monkeypatch):
    client = walmart_client()
    calls = []
    monkeypatch.setattr(
        client,
        "request",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"feedId": "feed-1"},
    )

    assert client.submit_inventory_feed({"A": 7}) == "feed-1"
    args, kwargs = calls[0]
    assert args == ("POST", "/v3/feeds")
    assert kwargs["params"] == {"feedType": "inventory"}
    assert json.loads(kwargs["content"]) == {
        "InventoryHeader": {"version": "1.4"},
        "Inventory": [{"sku": "A", "quantity": {"unit": "EACH", "amount": 7}}],
    }


def test_inventory_feed_rejects_payload_over_limit(monkeypatch):
    client = walmart_client()
    monkeypatch.setattr(client, "FEED_SIZE_LIMIT", 10)

    with pytest.raises(ApiError, match="5 MB"):
        client.submit_inventory_feed({"A": 7})


def test_inventory_feed_batches_stay_under_limit(monkeypatch):
    client = walmart_client()
    one_item_size = len(client._encoded_inventory_feed({"A": 1}))
    monkeypatch.setattr(client, "FEED_SIZE_LIMIT", one_item_size + 1)

    batches = client.inventory_feed_batches({"A": 1, "B": 2, "C": 3})

    assert batches == [{"A": 1}, {"B": 2}, {"C": 3}]
    assert all(len(client._encoded_inventory_feed(batch)) <= client.FEED_SIZE_LIMIT for batch in batches)


def test_inventory_feed_batches_reject_single_oversized_sku(monkeypatch):
    client = walmart_client()
    monkeypatch.setattr(client, "FEED_SIZE_LIMIT", 10)

    with pytest.raises(ApiError, match="SKU A"):
        client.inventory_feed_batches({"A": 1})


def test_feed_status_uses_summary_and_error_endpoints(monkeypatch):
    client = walmart_client()
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/v3/feeds":
            return {
                "payload": {
                    "results": {
                        "feed": [{
                            "feedStatus": "PROCESSED",
                            "itemsSucceeded": 1,
                            "itemsFailed": 1,
                        }]
                    }
                }
            }
        return {
            "payload": {
                "results": [{
                    "itemId": "FAIL",
                    "itemStatus": "DATA_ERROR",
                    "error": ['{"description":"SKU inválido"}'],
                }]
            }
        }

    monkeypatch.setattr(client, "request", fake_request)

    status = client.feed_status("feed-1")

    assert status.terminal
    assert status.item_statuses["FAIL"] == ("DATA_ERROR", "SKU inválido")
    assert calls[0] == ("GET", "/v3/feeds", {"params": {"feedId": "feed-1"}})
    assert calls[1][1] == "/v3/feeds/error/feed-1/items"
    assert calls[1][2] == {"params": {"limit": 50, "offset": 0}}


def test_feed_status_parses_real_walmart_feedid_response(monkeypatch):
    client = walmart_client()
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return real_feed_status_payload()

    monkeypatch.setattr(client, "request", fake_request)

    status = client.feed_status(
        "18BCA6ACB0C05E08B03CEC887C63D292@AUoBCgA",
        fetch_errors=False,
    )

    assert status.available is True
    assert status.terminal is True
    assert status.status == "PROCESSED"
    assert status.items_received == 2786
    assert status.items_succeeded == 435
    assert status.items_failed == 2351
    assert status.item_statuses == {}
    assert calls == [("GET", "/v3/feeds", {"params": {"feedId": "18BCA6ACB0C05E08B03CEC887C63D292@AUoBCgA"}})]


@pytest.mark.parametrize("payload", [
    {},
    {"payload": {"results": {"feed": []}}},
    {"feeds": [{"feedId": "another-feed", "feedStatus": "PROCESSED"}]},
    {
        "status": "OK",
        "header": {"headerAttributes": {}},
        "errors": [],
        "payload": {
            "totalResults": 0,
            "offset": 0,
            "limit": 50,
            "results": {},
        },
    },
])
def test_feed_status_treats_missing_feed_as_temporarily_unavailable(monkeypatch, payload):
    client = walmart_client()
    monkeypatch.setattr(client, "request", lambda *_args, **_kwargs: payload)

    status = client.feed_status("feed-1")

    assert status.available is False
    assert status.terminal is False


def test_feed_status_accepts_direct_feed_object(monkeypatch):
    client = walmart_client()
    monkeypatch.setattr(
        client,
        "request",
        lambda *_args, **_kwargs: {
            "feedId": "feed-1",
            "feedStatus": "PROCESSED",
            "itemsSucceeded": 2,
            "itemsFailed": 0,
        },
    )

    status = client.feed_status("feed-1")

    assert status.available
    assert status.terminal
    assert status.items_succeeded == 2


def test_feed_status_reports_sanitized_unknown_response(monkeypatch):
    client = walmart_client()
    monkeypatch.setattr(
        client,
        "request",
        lambda *_args, **_kwargs: {
            "payload": {
                "unexpected": {"accessToken": "secret-token", "value": "diagnostic"}
            }
        },
    )

    with pytest.raises(ApiError) as error:
        client.feed_status("feed-1")

    assert "formato desconocido" in str(error.value)
    assert "diagnostic" in str(error.value)
    assert "secret-token" not in str(error.value)
    assert "[REDACTED]" in str(error.value)


def test_feed_errors_reads_all_pages(monkeypatch):
    client = walmart_client()
    details = [
        {
            "itemId": f"SKU-{index}",
            "itemStatus": "DATA_ERROR",
            "error": [{"description": "SKU inexistente"}],
        }
        for index in range(51)
    ]
    offsets = []

    def fake_request(_method, _path, **kwargs):
        offset = kwargs["params"]["offset"]
        limit = kwargs["params"]["limit"]
        offsets.append(offset)
        return {"payload": {"results": details[offset:offset + limit]}}

    monkeypatch.setattr(client, "request", fake_request)

    errors = client.feed_errors("feed-1")

    assert len(errors) == 51
    assert offsets == [0, 50]
