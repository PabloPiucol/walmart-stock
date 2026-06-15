from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import json
import time
from typing import Any
from uuid import uuid4

import httpx


class ApiError(RuntimeError):
    pass


class AuthenticationError(ApiError):
    def __init__(
        self,
        operation: str,
        detail: str,
        *,
        status_code: int | None = None,
        correlation_id: str = "",
    ):
        status = f" HTTP {status_code}" if status_code is not None else ""
        correlation = f" [correlation_id={correlation_id}]" if correlation_id else ""
        super().__init__(f"{operation}:{status}{correlation}: {detail}")
        self.status_code = status_code
        self.correlation_id = correlation_id


class RateLimitError(ApiError):
    def __init__(self, method: str, path: str, detail: str, retry_after: float | None = None):
        super().__init__(f"{method} {path}: HTTP 429: {detail}")
        self.retry_after = retry_after


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


def sanitized_summary(value: Any) -> str:
    sensitive = ("token", "secret", "authorization", "credential", "password")

    def sanitize(current: Any, depth: int = 0) -> Any:
        if depth > 5:
            return "..."
        if isinstance(current, dict):
            return {
                str(key): (
                    "[REDACTED]"
                    if any(word in str(key).lower() for word in sensitive)
                    else sanitize(child, depth + 1)
                )
                for key, child in list(current.items())[:20]
            }
        if isinstance(current, list):
            return [sanitize(child, depth + 1) for child in current[:10]]
        if isinstance(current, str):
            return current[:200]
        return current

    return json.dumps(sanitize(value), ensure_ascii=False, separators=(",", ":"))[:1000]


def _response_summary(response: httpx.Response | None) -> str:
    if response is None or not response.content:
        return ""
    try:
        return sanitized_summary(response.json())
    except ValueError:
        return sanitized_summary({"body": response.text[:500]})


class BaseApiClient:
    def __init__(self, base_url: str, headers: dict[str, str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(30),
        )

    def request(self, method: str, path: str, **kwargs) -> Any:
        for attempt in range(3):
            try:
                response = self.client.request(method, path, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < 2:
                        try:
                            delay = min(float(response.headers.get("retry-after", attempt + 1)), 5)
                        except ValueError:
                            delay = attempt + 1
                        time.sleep(delay)
                        continue
                response.raise_for_status()
                return response.json() if response.content else {}
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text[:500]
                raise ApiError(f"{method} {path}: HTTP {exc.response.status_code}: {detail}") from exc
            except httpx.HTTPError as exc:
                if attempt < 2:
                    time.sleep(attempt + 1)
                    continue
                raise ApiError(f"{method} {path}: {exc}") from exc
        raise ApiError(f"{method} {path}: error desconocido")


class BsaleClient(BaseApiClient):
    def __init__(self, base_url: str, access_token: str):
        super().__init__(base_url, {"access_token": access_token})

    def paginated(self, path: str, params: dict[str, Any] | None = None) -> list[dict]:
        result: list[dict] = []
        offset = 0
        limit = 50
        while True:
            page_params = {**(params or {}), "limit": limit, "offset": offset}
            payload = self.request("GET", path, params=page_params)
            items = payload.get("items", [])
            result.extend(items)
            if len(items) < limit or offset + limit >= payload.get("count", 0):
                return result
            offset += limit

    def offices(self) -> list[dict]:
        return self.paginated("/offices.json")

    def variants(self) -> list[dict]:
        return self.paginated("/variants.json")

    def stocks(self, office_id: str) -> list[dict]:
        stocks = self.paginated("/stocks.json", {"officeid": office_id})
        return [
            stock for stock in stocks
            if str((stock.get("office") or {}).get("id", "")) == str(office_id)
        ]


@dataclass(frozen=True)
class WalmartFeedStatus:
    status: str
    item_statuses: dict[str, tuple[str, str]]
    items_succeeded: int
    items_failed: int
    available: bool = True

    @property
    def terminal(self) -> bool:
        return self.status.upper() in {"PROCESSED", "ERROR", "FAILED"}


class WalmartAuthenticator:
    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        partner_id: str = "",
        channel_type: str = "",
        market: str = "cl",
    ):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.partner_id = partner_id
        self.channel_type = channel_type
        self.market = market
        self.client = httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(30))
        self._access_token = ""
        self._token_expires_at = 0.0
        self.last_token_diagnostic: dict[str, Any] = {}

    def _correlation_id(self) -> str:
        return str(uuid4())

    def _token_headers(self, correlation_id: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "WM_MARKET": self.market,
            "WM_QOS.CORRELATION_ID": correlation_id,
            "WM_SVC.NAME": "Walmart Marketplace",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if self.partner_id:
            headers["WM_PARTNER.ID"] = self.partner_id
        if self.channel_type:
            headers["WM_CONSUMER.CHANNEL.TYPE"] = self.channel_type
        return headers

    def business_headers(self, token: str, correlation_id: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "WM_MARKET": self.market,
            "WM_QOS.CORRELATION_ID": correlation_id,
            "WM_SEC.ACCESS_TOKEN": token,
            "WM_SVC.NAME": "Walmart Marketplace",
        }
        if self.channel_type:
            headers["WM_CONSUMER.CHANNEL.TYPE"] = self.channel_type
        return headers

    def invalidate(self) -> None:
        self._access_token = ""
        self._token_expires_at = 0.0

    def _validate_config(self) -> None:
        missing = [
            name
            for name, value in (
                ("WALMART_CLIENT_ID", self.client_id),
                ("WALMART_CLIENT_SECRET", self.client_secret),
            )
            if not value
        ]
        if missing:
            raise AuthenticationError(
                "Configuración Walmart",
                f"Faltan {', '.join(missing)}",
            )

    def access_token(self, *, force: bool = False) -> str:
        self._validate_config()
        if not force and self._access_token and time.monotonic() < self._token_expires_at:
            return self._access_token
        for attempt in range(3):
            response: httpx.Response | None = None
            correlation_id = self._correlation_id()
            try:
                response = self.client.post(
                    "/v3/token",
                    auth=(self.client_id, self.client_secret),
                    data={"grant_type": "client_credentials"},
                    headers=self._token_headers(correlation_id),
                )
                if response.status_code == 429:
                    if attempt < 2:
                        time.sleep(_retry_after_seconds(response.headers.get("retry-after")) or attempt + 1)
                        continue
                    raise RateLimitError(
                        "POST",
                        "/v3/token",
                        response.text[:500],
                        _retry_after_seconds(response.headers.get("retry-after")),
                    )
                if response.status_code >= 500 and attempt < 2:
                    time.sleep(attempt + 1)
                    continue
                if response.status_code in {400, 401, 403}:
                    raise AuthenticationError(
                        "POST /v3/token",
                        _response_summary(response),
                        status_code=response.status_code,
                        correlation_id=correlation_id,
                    )
                response.raise_for_status()
                payload = response.json()
                self._access_token = str(payload["access_token"])
                reported_expires_in = int(payload.get("expires_in", 900))
                self._token_expires_at = time.monotonic() + max(0, reported_expires_in - 60)
                self.last_token_diagnostic = {
                    "status": "emitted",
                    "expires_in": reported_expires_in,
                    "token_type": payload.get("token_type", ""),
                    "correlation_id": correlation_id,
                }
                return self._access_token
            except AuthenticationError:
                raise
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                if isinstance(exc, httpx.RequestError) and attempt < 2:
                    time.sleep(attempt + 1)
                    continue
                detail = _response_summary(response) or str(exc)
                raise AuthenticationError(
                    "POST /v3/token",
                    detail,
                    status_code=response.status_code if response is not None else None,
                    correlation_id=correlation_id,
                ) from exc
        raise AuthenticationError("POST /v3/token", "error desconocido")


class WalmartClient:
    FEED_SIZE_LIMIT = 5 * 1024 * 1024

    def __init__(self, authenticator: WalmartAuthenticator):
        self.authenticator = authenticator
        self.base_url = authenticator.base_url
        self.client = httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(30))

    def request(self, method: str, path: str, **kwargs) -> Any:
        custom_headers = kwargs.pop("headers", {})
        auth_retry_used = False
        for attempt in range(3):
            headers = self.authenticator.business_headers(
                self.authenticator.access_token(),
                str(uuid4()),
            )
            headers.update(custom_headers)
            try:
                response = self.client.request(method, path, headers=headers, **kwargs)
                if response.status_code == 401 and not auth_retry_used:
                    auth_retry_used = True
                    self.authenticator.invalidate()
                    self.authenticator.access_token(force=True)
                    continue
                if response.status_code == 429:
                    raise RateLimitError(
                        method,
                        path,
                        response.text[:500],
                        _retry_after_seconds(response.headers.get("retry-after")),
                    )
                if response.status_code >= 500 and attempt < 2:
                    try:
                        delay = min(float(response.headers.get("retry-after", attempt + 1)), 5)
                    except ValueError:
                        delay = attempt + 1
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                return response.json() if response.content else {}
            except httpx.HTTPStatusError as exc:
                detail = exc.response.text[:500]
                raise ApiError(f"{method} {path}: HTTP {exc.response.status_code}: {detail}") from exc
            except httpx.HTTPError as exc:
                if attempt < 2:
                    time.sleep(attempt + 1)
                    continue
                raise ApiError(f"{method} {path}: {exc}") from exc
        raise ApiError(f"{method} {path}: error desconocido")

    @staticmethod
    def _inventory_feed_entry(sku: str, quantity: int) -> dict:
        return {
            "sku": sku,
            "quantity": {"unit": "EACH", "amount": quantity},
        }

    @classmethod
    def inventory_feed_payload(cls, quantities: dict[str, int]) -> dict:
        return {
            "InventoryHeader": {"version": "1.4"},
            "Inventory": [
                cls._inventory_feed_entry(sku, quantity)
                for sku, quantity in quantities.items()
            ],
        }

    def _encoded_inventory_feed(self, quantities: dict[str, int]) -> bytes:
        return json.dumps(
            self.inventory_feed_payload(quantities),
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()

    def inventory_feed_batches(self, quantities: dict[str, int]) -> list[dict[str, int]]:
        batches: list[dict[str, int]] = []
        current: dict[str, int] = {}
        base_size = len(self._encoded_inventory_feed({}))
        current_size = base_size
        for sku, quantity in quantities.items():
            encoded_entry = json.dumps(
                self._inventory_feed_entry(sku, quantity),
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
            additional_size = len(encoded_entry) + (1 if current else 0)
            if current_size + additional_size <= self.FEED_SIZE_LIMIT:
                current[sku] = quantity
                current_size += additional_size
                continue
            if not current:
                raise ApiError(f"El SKU {sku} no cabe en un feed de inventario de 5 MB")
            batches.append(current)
            current = {sku: quantity}
            current_size = base_size + len(encoded_entry)
            if current_size > self.FEED_SIZE_LIMIT:
                raise ApiError(f"El SKU {sku} no cabe en un feed de inventario de 5 MB")
        if current:
            batches.append(current)
        return batches

    def submit_inventory_feed(self, quantities: dict[str, int]) -> str:
        encoded = self._encoded_inventory_feed(quantities)
        if len(encoded) > self.FEED_SIZE_LIMIT:
            raise ApiError("El feed de inventario supera el límite preventivo de 5 MB")
        response = self.request(
            "POST",
            "/v3/feeds",
            params={"feedType": "inventory"},
            content=encoded,
            headers={"Content-Type": "application/json"},
        )
        feed_id = response.get("feedId")
        if not feed_id:
            raise ApiError("Walmart no devolvió feedId")
        return str(feed_id)

    @staticmethod
    def _feed_entries(value: Any) -> list[dict]:
        if isinstance(value, dict):
            entries = [value] if "feedId" in value or "feedStatus" in value else []
            for child in value.values():
                entries.extend(WalmartClient._feed_entries(child))
            return entries
        if isinstance(value, list):
            entries = []
            for child in value:
                entries.extend(WalmartClient._feed_entries(child))
            return entries
        return []

    @staticmethod
    def _contains_feed_collection(value: Any) -> bool:
        if isinstance(value, dict):
            if any(key in value for key in ("feed", "feeds")):
                return True
            return any(WalmartClient._contains_feed_collection(child) for child in value.values())
        if isinstance(value, list):
            return any(WalmartClient._contains_feed_collection(child) for child in value)
        return False

    @staticmethod
    def _is_known_empty_feed_response(value: Any) -> bool:
        if value in (None, "", [], {}):
            return True
        if not isinstance(value, dict):
            return False
        allowed_metadata = {"totalResults", "limit", "offset"}
        for key, child in value.items():
            if key in allowed_metadata:
                continue
            if key not in {"payload", "results", "feed", "feeds"}:
                return False
            if not WalmartClient._is_known_empty_feed_response(child):
                return False
        return True

    def feed_status(self, feed_id: str) -> WalmartFeedStatus:
        payload = self.request("GET", "/v3/feeds", params={"feedId": feed_id})
        feeds = self._feed_entries(payload)
        feed = next(
            (entry for entry in feeds if str(entry.get("feedId") or "") == feed_id),
            None,
        )
        if feed is None and len(feeds) == 1 and not feeds[0].get("feedId"):
            feed = feeds[0]
        if feed is None:
            if feeds or self._contains_feed_collection(payload) or self._is_known_empty_feed_response(payload):
                return WalmartFeedStatus("", {}, 0, 0, available=False)
            raise ApiError(
                f"Walmart devolvió un formato desconocido para el feed {feed_id}: "
                f"{sanitized_summary(payload)}"
            )
        status = str(feed.get("feedStatus") or "")
        failed = int(feed.get("itemsFailed") or 0)
        return WalmartFeedStatus(
            status=status,
            item_statuses=self.feed_errors(feed_id)
            if failed and status.upper() in {"PROCESSED", "ERROR", "FAILED"}
            else {},
            items_succeeded=int(feed.get("itemsSucceeded") or 0),
            items_failed=failed,
        )

    def feed_errors(self, feed_id: str) -> dict[str, tuple[str, str]]:
        result: dict[str, tuple[str, str]] = {}
        offset = 0
        limit = 50
        while True:
            payload = self.request(
                "GET",
                f"/v3/feeds/error/{feed_id}/items",
                params={"limit": limit, "offset": offset},
            )
            details = (payload.get("payload") or {}).get("results") or []
            before_count = len(result)
            for detail in details:
                sku = str(detail.get("itemId") or "").strip()
                if not sku:
                    continue
                errors = detail.get("errors") or detail.get("error") or []
                if not isinstance(errors, list):
                    errors = [errors]
                messages = []
                for error in errors:
                    if isinstance(error, str):
                        try:
                            error = json.loads(error)
                        except json.JSONDecodeError:
                            messages.append(error)
                            continue
                    messages.append(str(error.get("description") or error.get("message") or error))
                status = str(detail.get("itemStatus") or "ERROR").upper()
                result[sku] = (status, "; ".join(messages))
            if len(details) < limit or len(result) == before_count:
                return result
            offset += len(details)
