"""FastAPI 의존성 — 현재 사용자 / 역할 게이트."""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ROLE_ADMIN, ROLE_REVIEWER, User


def _session_uid(request: Request) -> int | None:
    v = request.session.get("user_id")
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def get_current_user(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> User:
    uid = _session_uid(request)
    if uid is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.get(User, uid)
    if not user or not user.is_active:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    if user.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="관리자 전용")
    return user


def require_reviewer(user: Annotated[User, Depends(get_current_user)]) -> User:
    """계측 화면은 리뷰어 전용.

    관리자가 계측에 참여하면 관찰자간 일치도 집계가 오염되므로 역할을 엄격히 나눈다
    (ABDXAI 서버가 같은 이유로 admin의 라벨링 클라이언트 사용을 막는다).
    """
    if user.role != ROLE_REVIEWER:
        raise HTTPException(status_code=403, detail="계측자 전용")
    return user


def reviewer_remaining(db: Session, user: User) -> int:
    """이 계측자가 실제 계측 pool 에서 아직 답하지 않은 (영상 × 항목) 수.

    `app/routers/reviewer.py` 를 import 하면 순환이 되므로(그쪽이 이 모듈을
    쓴다) 같은 계산을 여기서 직접 한다 — 전체 쌍 수 − 내가 끝낸 수.
    """
    from sqlalchemy import func, select as _select

    from app.models import M_DONE, POOL_REAL, Image, Measurement
    from app.variables import N_VARIABLES

    n_images = db.scalar(
        _select(func.count()).select_from(Image)
        .where(Image.status == "active", Image.pool == POOL_REAL)
    ) or 0
    done = db.scalar(
        _select(func.count()).select_from(Measurement)
        .join(Image, Image.id == Measurement.image_id)
        .where(
            Measurement.user_id == user.id,
            Measurement.status == M_DONE,
            Image.pool == POOL_REAL,
        )
    ) or 0
    return max(0, int(n_images) * N_VARIABLES - int(done))


def matrix_view_open(db: Session, user: User) -> bool:
    """이 사용자에게 비교 화면(매트릭스·영상 상세)이 열려 있는가.

    * 관리자 — 항상 열림
    * 계측자 — **관리자가 공개를 켰고** 남은 작업이 0일 때만

    왜 두 조건인가 — 공개 스위치 없이 '남은 작업 0' 만 보면, 아직 업로드하지
    않은 영상이 있을 때도 열린다. 실제로 test set 519장 중 159장만 올라간
    상태에서 두 계측자의 남은 작업이 0이었다. 그때 열어 주면 서로의 값과 AI
    예측을 본 뒤 남은 350장을 재게 되어 **그 계측이 전부 오염된다.**
    여는 시점은 관리자가 판단해야 한다.
    """
    from app import settings_store

    if user.role == ROLE_ADMIN:
        return True
    if user.role != ROLE_REVIEWER:
        return False
    if not settings_store.reviewer_review_access(db):
        return False
    return reviewer_remaining(db, user) == 0


def require_matrix_view(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """비교 화면 접근 게이트. 관리자이거나, 공개된 '계측을 끝낸' 계측자."""
    if matrix_view_open(db, user):
        return user
    from app import settings_store

    if user.role == ROLE_REVIEWER and not settings_store.reviewer_review_access(db):
        raise HTTPException(403, "비교 화면은 아직 공개되지 않았습니다.")
    if user.role == ROLE_REVIEWER:
        n = reviewer_remaining(db, user)
        raise HTTPException(
            403, f"남은 계측 {n}건을 마치면 비교 화면이 열립니다."
        )
    raise HTTPException(403, "권한이 없습니다.")


CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
ReviewerUser = Annotated[User, Depends(require_reviewer)]
MatrixViewer = Annotated[User, Depends(require_matrix_view)]
DbSession = Annotated[Session, Depends(get_db)]
