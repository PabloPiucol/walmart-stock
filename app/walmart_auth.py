from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.clients import WalmartAuthenticator, sanitized_summary
from app.config import AppConfig, get_config
from app.models import Setting


AUTH_STATUS_KEY = "walmart_auth_last_status"
AUTH_CHECKED_AT_KEY = "walmart_auth_last_checked_at"
AUTH_SUMMARY_KEY = "walmart_auth_last_summary"


def walmart_configured(config: AppConfig | None = None) -> bool:
    config = config or get_config()
    return bool(
        config.walmart_client_id
        and config.walmart_client_secret
        and config.walmart_partner_id
    )


def walmart_authenticator(config: AppConfig | None = None) -> WalmartAuthenticator:
    config = config or get_config()
    return WalmartAuthenticator(
        config.walmart_api_url,
        config.walmart_client_id,
        config.walmart_client_secret,
        config.walmart_partner_id,
        config.walmart_channel_type,
        config.walmart_market,
    )


def _set_values(db: Session, values: dict[str, str]) -> None:
    for key, value in values.items():
        setting = db.get(Setting, key)
        if setting:
            setting.value = value
        else:
            db.add(Setting(key=key, value=value))
    db.commit()


def record_auth_diagnostic(db: Session, success: bool, summary: str) -> None:
    _set_values(db, {
        AUTH_STATUS_KEY: "ok" if success else "error",
        AUTH_CHECKED_AT_KEY: datetime.now(UTC).isoformat(),
        AUTH_SUMMARY_KEY: summary[:2000],
    })


def validate_walmart_auth(
    db: Session,
    authenticator: WalmartAuthenticator | None = None,
) -> tuple[WalmartAuthenticator, object]:
    authenticator = authenticator or walmart_authenticator()
    try:
        detail = authenticator.authenticate_and_validate()
    except Exception as exc:
        summary = str(exc)
        known_secrets = {
            getattr(authenticator, name, "")
            for name in ("client_id", "client_secret", "partner_id", "_access_token")
        }
        for secret in sorted(known_secrets, key=len, reverse=True):
            if secret:
                summary = summary.replace(secret, "[REDACTED]")
        record_auth_diagnostic(db, False, summary)
        raise
    record_auth_diagnostic(db, True, sanitized_summary(detail))
    return authenticator, detail


def walmart_auth_diagnostic(db: Session) -> dict[str, str]:
    def value(key: str) -> str:
        setting = db.get(Setting, key)
        return setting.value if setting else ""

    return {
        "status": value(AUTH_STATUS_KEY),
        "checked_at": value(AUTH_CHECKED_AT_KEY),
        "summary": value(AUTH_SUMMARY_KEY),
    }
