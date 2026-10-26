"""최초 기동 시 관리자 계정 확보."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.config import INITIAL_ADMIN_PASSWORD, INITIAL_ADMIN_USERNAME
from app.models import ROLE_ADMIN, User


def ensure_admin_user(db: Session) -> None:
    """관리자가 하나도 없으면 .env의 초기 계정을 만든다.

    `must_change_password=True`라서 첫 로그인 시 비밀번호 변경 화면으로 강제된다.
    이미 관리자가 있으면 아무것도 하지 않는다 — 운영 중 .env를 건드려도 안전.
    """
    has_admin = db.scalar(
        select(User.id).where(User.role == ROLE_ADMIN, User.is_active.is_(True)).limit(1)
    )
    if has_admin:
        return

    existing = db.scalar(
        select(User).where(User.username == INITIAL_ADMIN_USERNAME)
    )
    if existing is not None:
        # 같은 이름의 비활성/리뷰어 계정이 있으면 관리자로 승격해 잠금 상태를 푼다
        existing.role = ROLE_ADMIN
        existing.is_active = True
        db.commit()
        return

    # 여기서 처음으로 비밀번호가 쓰인다 — 검사도 여기여야 한다.
    # 관리자가 이미 있거나 승격으로 끝나는 경우엔 .env 가 비어 있어도 된다.
    _require_usable_initial_password()

    db.add(
        User(
            username=INITIAL_ADMIN_USERNAME,
            full_name="관리자",
            password_hash=hash_password(INITIAL_ADMIN_PASSWORD),
            role=ROLE_ADMIN,
            is_active=True,
            must_change_password=True,
        )
    )
    db.commit()
    print(
        f"[bootstrap] 초기 관리자 생성: {INITIAL_ADMIN_USERNAME} "
        f"(첫 로그인 시 비밀번호 변경 필요)"
    )

_WEAK = {"admin", "password", "picc", "test", "1234", "12345678",
         "changeme", "admin1234", "administrator"}


def _require_usable_initial_password() -> None:
    """초기 관리자 비밀번호가 없거나 뻔하면 **기동을 거부**한다.

    기본값으로 조용히 계정을 만들면, 배포한 사람이 그 사실을 모른 채 열어 두게
    된다. 환자 영상을 다루는 도구에서 그건 받아들일 수 없다. 이미 관리자가
    있는 운영 환경은 이 검사에 걸리지 않는다 — `.env` 를 비워 둬도 된다.
    """
    pw = (INITIAL_ADMIN_PASSWORD or "").strip()
    if not pw:
        raise RuntimeError(
            "INITIAL_ADMIN_PASSWORD 가 비어 있습니다. .env 에 초기 관리자 "
            "비밀번호를 넣고 다시 띄우세요 (첫 로그인 시 변경이 강제됩니다)."
        )
    if len(pw) < 8 or pw.lower() in _WEAK:
        raise RuntimeError(
            "INITIAL_ADMIN_PASSWORD 가 너무 약합니다 — 8자 이상이어야 하고 "
            "흔한 값은 쓸 수 없습니다."
        )
