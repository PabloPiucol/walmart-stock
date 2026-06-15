from types import SimpleNamespace

import pytest

from app.walmart_auth import (
    obtain_walmart_token,
    walmart_auth_diagnostic,
    walmart_configured,
)
from tests.test_sync_service import sessions


def test_walmart_configured_does_not_require_partner_id():
    complete = SimpleNamespace(
        walmart_client_id="client",
        walmart_client_secret="secret",
        walmart_partner_id="",
    )
    incomplete = SimpleNamespace(
        walmart_client_id="",
        walmart_client_secret="secret",
        walmart_partner_id="partner",
    )

    assert walmart_configured(complete) is True
    assert walmart_configured(incomplete) is False


def test_token_obtention_persists_safe_success_diagnostic():
    class FakeAuthenticator:
        client_id = "client"
        client_secret = "secret"
        partner_id = "partner"
        _access_token = "token"
        last_token_diagnostic = {}

        def access_token(self):
            self.last_token_diagnostic = {
                "status": "emitted",
                "expires_in": 900,
                "correlation_id": "correlation",
            }
            return "token"

    with sessions()() as db:
        obtain_walmart_token(db, FakeAuthenticator())
        diagnostic = walmart_auth_diagnostic(db)

    assert diagnostic["status"] == "ok"
    assert diagnostic["checked_at"]
    assert diagnostic["summary"] == (
        '{"status":"emitted","expires_in":900,"correlation_id":"correlation"}'
    )
    assert "token" not in diagnostic["summary"]


def test_token_obtention_persists_failure_without_known_secrets():
    class FakeAuthenticator:
        client_id = "client-id"
        client_secret = "client-secret"
        partner_id = "partner-id"
        _access_token = "access-token"

        def access_token(self):
            raise RuntimeError(
                "failure client-id client-secret partner-id access-token"
            )

    with sessions()() as db:
        with pytest.raises(RuntimeError):
            obtain_walmart_token(db, FakeAuthenticator())
        diagnostic = walmart_auth_diagnostic(db)

    assert diagnostic["status"] == "error"
    assert diagnostic["checked_at"]
    assert diagnostic["summary"] == (
        "failure [REDACTED] [REDACTED] [REDACTED] [REDACTED]"
    )
