"""테스트 데이터 초기화.

테스트를 충분히 해 본 뒤 실전 데이터로 넘어갈 때 쓴다. 되돌릴 수 없으므로
호출 전에 **관리자 본인의 비밀번호**와 **확인 문구**를 둘 다 검증한다
(`routers/admin.py`의 `/admin/reset`).

범위는 좁은 것부터 넓은 것까지 셋이다. 넓은 범위는 좁은 범위를 포함한다.
관리자 계정은 어떤 범위에서도 지우지 않는다 — 지우면 아무도 못 들어온다.
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import DATA_DIR, DICOM_DIR, PNG_DIR
from app.models import (
    ROLE_REVIEWER,
    Image,
    Measurement,
    Setting,
    User,
)

SCOPE_MEASUREMENTS = "measurements"
SCOPE_IMAGES = "images"
SCOPE_ALL = "all"


@dataclass(frozen=True)
class ResetScope:
    key: str
    title: str
    description: str
    keeps: str
    phrase: str          # 사용자가 그대로 타이핑해야 하는 확인 문구


SCOPES: dict[str, ResetScope] = {
    SCOPE_MEASUREMENTS: ResetScope(
        key=SCOPE_MEASUREMENTS,
        title="계측 기록만 초기화",
        description="모든 계측값과 확정값을 지웁니다. "
                    "업로드한 영상과 계정은 그대로 두므로, 같은 영상으로 처음부터 다시 계측할 수 있습니다.",
        keeps="영상 · 계정 · 설정",
        phrase="계측 기록 삭제",
    ),
    SCOPE_IMAGES: ResetScope(
        key=SCOPE_IMAGES,
        title="영상까지 초기화",
        description="위에 더해 등재된 영상을 전부 지웁니다. "
                    "원본 DICOM과 렌더 PNG 파일도 디스크에서 삭제됩니다.",
        keeps="계정 · 설정",
        phrase="영상까지 삭제",
    ),
    SCOPE_ALL: ResetScope(
        key=SCOPE_ALL,
        title="전체 초기화",
        description="위에 더해 계측자 계정과 설정값까지 지웁니다. "
                    "관리자 계정만 남으므로 처음 설치한 상태와 같아집니다.",
        keeps="관리자 계정",
        phrase="전체 삭제",
    ),
}

# 넓은 범위가 포함하는 좁은 범위
_INCLUDES = {
    SCOPE_MEASUREMENTS: (SCOPE_MEASUREMENTS,),
    SCOPE_IMAGES: (SCOPE_MEASUREMENTS, SCOPE_IMAGES),
    SCOPE_ALL: (SCOPE_MEASUREMENTS, SCOPE_IMAGES, SCOPE_ALL),
}


def preview_counts(db: Session) -> dict[str, int]:
    """무엇이 얼마나 지워지는지. 실행 전 화면에 그대로 보여 준다."""
    def n(model) -> int:
        return int(db.scalar(select(func.count()).select_from(model)) or 0)

    return {
        "measurements": n(Measurement),
        "images": n(Image),
        "reviewers": int(
            db.scalar(
                select(func.count()).select_from(User).where(User.role == ROLE_REVIEWER)
            ) or 0
        ),
        "settings": n(Setting),
    }


def backup_database() -> Path | None:
    """초기화 직전에 DB를 통째로 복사해 둔다.

    초기화는 되돌릴 수 없다. 실수로든 사고로든 한 번 지우면 사람이 며칠 들여 만든
    계측이 사라진다 — 실제로 그렇게 날린 적이 있다. 그래서 확인 문구와 비밀번호를
    통과했더라도, 지우기 전에 스냅샷을 남긴다.

    `VACUUM INTO` 를 쓰면 WAL까지 반영된 일관된 사본이 한 파일로 떨어진다
    (파일 복사는 WAL이 따로 놀아 깨질 수 있다).
    """
    db_path = DATA_DIR / "app.db"
    if not db_path.exists():
        return None
    backup_dir = DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / f"app_{time.strftime('%Y%m%d_%H%M%S')}.db"
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("VACUUM INTO ?", (str(dest),))
        finally:
            conn.close()
    except sqlite3.Error:
        try:
            shutil.copy2(db_path, dest)   # 그래도 안 되면 단순 복사라도
        except OSError:
            return None
    return dest


def _wipe_media() -> int:
    """렌더 PNG와 원본 DICOM 파일을 지운다. 지운 최상위 폴더 수를 반환."""
    removed = 0
    for root in (DICOM_DIR, PNG_DIR):
        if not root.exists():
            continue
        for child in root.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                removed += 1
            except OSError:
                # 다른 프로세스가 잡고 있으면 건너뛴다 — DB는 이미 정리되므로
                # 남은 파일은 참조되지 않는 고아가 될 뿐이다
                pass
    return removed


def execute(db: Session, scope_key: str) -> dict[str, int]:
    """초기화 실행. 실제로 지운 행 수를 반환한다.

    호출자가 비밀번호와 확인 문구를 이미 검증했다고 가정한다.
    """
    scope = SCOPES.get(scope_key)
    if scope is None:
        raise ValueError(f"알 수 없는 초기화 범위: {scope_key}")

    steps = _INCLUDES[scope.key]
    before = preview_counts(db)
    result: dict[str, int] = {}

    # 지우기 전에 스냅샷. 실패해도 초기화 자체는 진행한다(관리자가 요청한 동작이므로)
    backup = backup_database()
    result["_backup"] = str(backup) if backup else ""

    # 항상: 계측 기록
    db.execute(delete(Measurement))
    result["measurements"] = before["measurements"]

    if SCOPE_IMAGES in steps:
        db.execute(delete(Image))
        result["images"] = before["images"]

    if SCOPE_ALL in steps:
        # 관리자는 남긴다. 지우면 아무도 로그인할 수 없다.
        db.execute(delete(User).where(User.role == ROLE_REVIEWER))
        db.execute(delete(Setting))
        result["reviewers"] = before["reviewers"]
        result["settings"] = before["settings"]

    db.commit()

    # 파일은 DB 커밋 이후에 지운다 — 중간에 실패해도 DB와 어긋나지 않게
    if SCOPE_IMAGES in steps:
        result["media_dirs"] = _wipe_media()

    return result
