"""런타임 설정 읽기/쓰기 — Setting 테이블 + config.DEFAULT_SETTINGS 폴백."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import DEFAULT_SETTINGS
from app.models import Setting


def get_raw(db: Session, key: str) -> str:
    row = db.get(Setting, key)
    if row is not None and row.value is not None:
        return row.value
    return DEFAULT_SETTINGS.get(key, "")


def get_float(db: Session, key: str) -> float:
    try:
        return float(get_raw(db, key))
    except (TypeError, ValueError):
        return float(DEFAULT_SETTINGS.get(key, "0") or 0)


def get_int(db: Session, key: str) -> int:
    try:
        return int(float(get_raw(db, key)))
    except (TypeError, ValueError):
        return int(float(DEFAULT_SETTINGS.get(key, "0") or 0))


def set_value(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=str(value)))
    else:
        row.value = str(value)
    db.commit()


def all_settings(db: Session) -> dict[str, str]:
    """기본값 위에 저장된 값을 덮어쓴 전체 설정."""
    merged = dict(DEFAULT_SETTINGS)
    for row in db.scalars(select(Setting)).all():
        merged[row.key] = row.value
    return merged


# 편의 접근자 -------------------------------------------------------------


def tolerance_mm(db: Session) -> float:
    return get_float(db, "tolerance_mm")


def render_max_px(db: Session) -> int:
    return get_int(db, "render_max_px")


def idle_lock_ms(db: Session) -> int:
    return get_int(db, "idle_lock_ms")


def idle_phase1_ms(db: Session) -> int:
    return get_int(db, "idle_phase1_ms")


def max_duration_ms(db: Session) -> int:
    return get_int(db, "max_duration_ms")


def reviewer_review_access(db: Session) -> bool:
    """계측자에게 비교 화면을 공개하는가 (기본 꺼짐).

    켜는 것만으로 열리지 않는다 — 남은 작업이 0인 계측자에게만 열린다
    (`app/deps.py::require_matrix_view`).
    """
    return get_raw(db, "reviewer_review_access").strip() in ("1", "true", "True", "on")
