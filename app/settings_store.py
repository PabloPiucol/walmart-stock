from sqlalchemy.orm import Session

from app.models import Setting


def get_setting(db: Session, key: str, default: str = "") -> str:
    setting = db.get(Setting, key)
    return setting.value if setting else default


def set_setting(db: Session, key: str, value: str) -> None:
    setting = db.get(Setting, key)
    if setting:
        setting.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()

