from types import SimpleNamespace

import pytest

from app.walmart_auth import (
    validate_walmart_auth,
    walmart_auth_diagnostic,
    walmart_configured,
)
from tests.test_sync_service import sessions


def test_walmart_configured_requires_partner_id():
    incomplete = SimpleNamespace(
        walmart_client_id="client",
        walmart_client_secret="secret",
        walmart_partner_id="",
    )
    complete = SimpleNamespace(
        walmart_client_id="client",
        walmart_client_secret="secret",
        walmart_partner_id="partner",
    )

    assert walmart_configured(incomplete) is False
    assert walmart_configured(complete) is True


def test_auth_validation_persists_sanitized_success_diagnostic():
    class FakeAuthenticator:
        client_id = "client"
        client_secret = "secret"
        partner_id = "partner"
        _access_token = "token"

        def authenticate_and_validate(self):
            return {"accessToken": "token", "isValid": True}

    with sessions()() as db:
        validate_walmart_auth(db, FakeAuthenticator())
        diagnostic = walmart_auth_diagnostic(db)

    assert diagnostic["status"] == "ok"
    assert diagnostic["checked_at"]
    assert diagnostic["summary"] == '{"accessToken":"[REDACTED]","isValid":true}'
    assert "token" not in diagnostic["summary"]


def test_auth_validation_persists_failure_without_known_secrets():
    class FakeAuthenticator:
        client_id = "client-id"
        client_secret = "client-secret"
        partner_id = "partner-id"
        _access_token = "access-token"

        def authenticate_and_validate(self):
            raise RuntimeError(
                "failure client-id client-secret partner-id access-token"
            )

    with sessions()() as db:
        with pytest.raises(RuntimeError):
            validate_walmart_auth(db, FakeAuthenticator())
        diagnostic = walmart_auth_diagnostic(db)

    assert diagnostic["status"] == "error"
    assert diagnostic["checked_at"]
    assert diagnostic["summary"] == (
        "failure [REDACTED] [REDACTED] [REDACTED] [REDACTED]"
    )
