"""계측자(전문의) 화면 — 문제 출제 · 답안 저장 · 이력 · 영상 전송.

출제 방식
---------
    문제 출제 (어떤 변수를 잴지 안내, 사진은 아직 안 보임)
      → 사진 공개                       ← 여기서 시간 측정 시작
      → 드래그 한 번으로 계측
      → 손을 뗀 순간                     ← 여기서 시간 측정 종료
      → 확인 팝업 (계측선을 가리지 않는 위치)
          · 확정        → 저장하고 바로 다음 문제
          · 재측정 요청 → 대기 pool로 돌려보내고 바로 다음 문제

확인을 고민한 시간은 측정에 포함되지 않는다. 재측정을 요청해도 그 자리에서 다시 재지
않는다 — 방금 본 화면을 기억한 채 다시 재면 편향이 생기기 때문이다.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app import settings_store
from app.agreement import active_reviewers
from app.config import DATA_DIR
from app.deps import DbSession, ReviewerUser
from app.dicom_io import DicomError, enhance_png
from app.geometry import normalize_points, value_mm
from app.models import (
    M_DONE,
    M_REDO,
    POOL_LABELS,
    POOL_PRACTICE,
    POOL_REAL,
    Image,
    Measurement,
    User,
)
from app.variables import (
    ALL_VARIABLE_KEYS,
    N_VARIABLES,
    VARIABLES,
    axis_note,
    is_valid_variable,
)

router = APIRouter()
_templates: Jinja2Templates | None = None

KST = timezone(timedelta(hours=9))


def set_templates(t: Jinja2Templates) -> None:
    global _templates
    _templates = t


def tpl() -> Jinja2Templates:
    assert _templates is not None
    return _templates


# ----------------------------------------------------------------------------
# 출제 pool
# ----------------------------------------------------------------------------


SESSION_POOL_KEY = "measure_pool"


def current_pool(request: Request) -> str:
    """이 계측자가 지금 어느 pool에서 문제를 받고 있는가.

    세션에 담아 둔다 — `/measure`, `/measure/next`, `/measure/answer` 가 모두
    같은 값을 봐야 하기 때문이다. 다만 **저장되는 계측의 pool은 세션이 아니라
    영상 자신이 결정한다**(`Image.pool`). 중간에 모드를 바꿔도 이미 낸 답이
    엉뚱한 pool로 들어가지 않는다.
    """
    v = request.session.get(SESSION_POOL_KEY)
    return POOL_PRACTICE if v == POOL_PRACTICE else POOL_REAL


def _active_image_ids(db: DbSession, pool: str) -> list[int]:
    return [
        int(i) for i in db.scalars(
            select(Image.id)
            .where(Image.status == "active", Image.pool == pool)
            .order_by(Image.id)
        ).all()
    ]


def _done_pairs(db: DbSession, user_id: int, pool: str) -> set[tuple[int, str]]:
    """이 계측자가 해당 pool에서 이미 유효하게 답한 (영상, 항목) 쌍."""
    rows = db.execute(
        select(Measurement.image_id, Measurement.variable_key)
        .join(Image, Image.id == Measurement.image_id)
        .where(
            Measurement.user_id == user_id,
            Measurement.status == M_DONE,
            Image.pool == pool,
        )
    ).all()
    return {(int(a), b) for a, b in rows}


def _pending_pairs(db: DbSession, user_id: int, pool: str) -> list[tuple[int, str]]:
    """해당 pool에서 아직 답하지 않은 (영상, 항목) 쌍 전체.

    영상 수 × 8이라 150장이면 1,200쌍 — 파이썬에서 전부 만들어도 무시할 만하다.
    SQL로 교차조인을 흉내 내는 것보다 읽기 쉽다.
    """
    done = _done_pairs(db, user_id, pool)
    return [
        (image_id, key)
        for image_id in _active_image_ids(db, pool)
        for key in ALL_VARIABLE_KEYS
        if (image_id, key) not in done
    ]


def _next_question(db: DbSession, user_id: int, pool: str) -> tuple[Image, str] | None:
    """다음 문제. 미계측 쌍 전체에서 완전 무작위로 하나.

    영상과 변수를 함께 섞는 이유: 같은 영상의 8개를 연달아 내면 앞서 찍은 점을
    기억한 채 다음 계측을 하게 되어(anchoring) 계측자 간 차이가 인위적으로 줄고,
    소요시간도 두 번째부터 짧아진다. 둘 다 논문 지표를 오염시킨다.
    """
    pending = _pending_pairs(db, user_id, pool)
    if not pending:
        return None
    image_id, key = random.choice(pending)
    image = db.get(Image, image_id)
    if image is None:
        return None
    return image, key


def _progress(db: DbSession, user_id: int, pool: str) -> dict[str, Any]:
    n_images = db.scalar(
        select(func.count()).select_from(Image)
        .where(Image.status == "active", Image.pool == pool)
    ) or 0
    total = int(n_images) * N_VARIABLES
    done = db.scalar(
        select(func.count()).select_from(Measurement)
        .join(Image, Image.id == Measurement.image_id)
        .where(
            Measurement.user_id == user_id,
            Measurement.status == M_DONE,
            Image.pool == pool,
        )
    ) or 0
    pct = 0.0 if total <= 0 else min(100.0, round(100.0 * done / total, 1))
    return {
        "total": total, "done": int(done),
        "remaining": max(0, total - int(done)), "pct": pct,
        "n_images": int(n_images), "n_variables": N_VARIABLES,
        "pool": pool, "pool_label": POOL_LABELS.get(pool, pool),
        "is_practice": pool == POOL_PRACTICE,
    }


def _peer_progress(db: DbSession, me_id: int) -> list[dict]:
    """동료 진행률. 값은 보여주지 않고 건수만 — 계측은 서로 눈가림 상태여야 한다.

    실제 계측 pool만 센다. 연습은 남의 진행과 비교할 성질이 아니다.
    """
    rows = []
    for u in active_reviewers(db):
        p = _progress(db, int(u.id), POOL_REAL)
        rows.append({
            "name": u.display_label(), "done": p["done"], "total": p["total"],
            "pct": p["pct"], "is_self": u.id == me_id,
        })
    rows.sort(key=lambda r: (-r["done"], r["name"]))
    return rows


def _question_payload(image: Image, variable_key: str) -> dict[str, Any]:
    v = VARIABLES[variable_key]
    return {
        "imageId": int(image.id),
        "variableKey": v.key,
        "display": v.display,
        "short": v.short,
        "instruction": v.instruction,
        "guide": v.guide,
        # 브리핑 화면용 — 논문 원문 병기 + 두 점의 위치 + 놓치기 쉬운 지점
        "paperEn": v.paper_en,
        "pointA": v.point_a,
        "pointB": v.point_b,
        "caution": v.caution,
        "axis": v.axis,
        "axisNote": axis_note(v.axis),
        "filename": image.original_filename,
        "origW": image.orig_w,
        "origH": image.orig_h,
        "renderScale": image.render_scale,
        "pixelSpacingMm": image.pixel_spacing_mm,
        "hasSpacing": image.has_spacing,
        "pngUrl": f"/image/{image.id}/png",
    }


# 계측 화면 우측 목록에 보낼 최대 건수.
# 150장 × 8 = 1,200문제라 전부 담으면 목록이 끝없이 길어지고, 옛 항목을 찾는
# 용도로도 쓸모가 없다. 최근 것만 짧게 보여주고 나머지는 「내 이력」으로 보낸다.
MEASURE_LIST_LIMIT = 20


def _recent_answers(
    db: DbSession, user_id: int, pool: str, limit: int = MEASURE_LIST_LIMIT
) -> list[dict]:
    """우측 누적 목록. 최근 것이 맨 위로.

    `limit`건만 보낸다. 그보다 오래된 것은 「내 이력」 화면에서 본다.
    """
    rows = db.scalars(
        select(Measurement)
        .join(Image, Image.id == Measurement.image_id)
        .where(
            Measurement.user_id == user_id,
            Measurement.status == M_DONE,
            Image.pool == pool,
        )
        .order_by(Measurement.created_at.desc(), Measurement.id.desc())
        .limit(limit)
    ).all()
    out = []
    for m in rows:
        v = VARIABLES.get(m.variable_key)
        img = db.get(Image, m.image_id)
        out.append({
            "id": int(m.id),
            "imageId": int(m.image_id),
            "filename": img.original_filename if img else "",
            "variableKey": m.variable_key,
            "short": v.short if v else m.variable_key,
            "display": v.display if v else m.variable_key,
            "valueMm": None if m.value_mm is None else round(m.value_mm, 1),
            "durationMs": m.duration_ms,
            "attempt": m.attempt,
            "atDate": _kst_date(m.created_at),
            "atTime": _kst_time(m.created_at),
            "at": _kst_label(m.created_at),
        })
    return out


def _to_kst(dt: datetime | None):
    """UTC 저장값을 KST로. 저장은 UTC, 표시는 KST로 일관되게 유지한다."""
    return None if dt is None else dt.replace(tzinfo=timezone.utc).astimezone(KST)


def _kst_date(dt: datetime | None) -> str:
    """`2026-09-15`. 날짜와 시각을 나눠 보내는 이유 — 완료 목록이 두 줄로

        2026-09-15
        14:23:05

    를 표시하기 때문이다. 1,200문제를 하루에 끝낼 수 없으므로, 어느 날 잰
    것인지가 시:분:초만큼이나 중요하다.
    """
    local = _to_kst(dt)
    return "" if local is None else local.strftime("%Y-%m-%d")


def _kst_time(dt: datetime | None) -> str:
    """`14:23:05`."""
    local = _to_kst(dt)
    return "" if local is None else local.strftime("%H:%M:%S")


def _kst_label(dt: datetime | None) -> str:
    """한 줄로 붙인 형태 — 툴팁과 일시정지 화면처럼 자리가 좁은 곳에서 쓴다."""
    local = _to_kst(dt)
    if local is None:
        return ""
    if local.date() == datetime.now(KST).date():
        return local.strftime("%H:%M:%S")
    return local.strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------------
# 계측 화면
# ----------------------------------------------------------------------------


@router.get("/measure", response_class=HTMLResponse)
def measure(
    request: Request,
    db: DbSession,
    me: ReviewerUser,
    review: Annotated[int | None, Query()] = None,
):
    """계측 화면.

    `?review=<measurement_id>` 로 들어오면 그 계측을 **열람 모드로 바로 연다** —
    「내 이력」에서 한 줄을 눌러 왔을 때다. 열람 화면에는 재측정 요청 버튼이 있고,
    닫으면 이력으로 되돌아간다.

    열람으로 열려도 뒤에는 새 문제가 준비돼 있다. 문제 카드를 지나야 타이머가
    돌기 시작하므로, 들여다보는 동안 소요시간이 오염되지 않는다.
    """
    pool = current_pool(request)
    progress = _progress(db, int(me.id), pool)
    question = _next_question(db, int(me.id), pool)

    if question is None:
        return tpl().TemplateResponse(
            "measure_done.html",
            {"request": request, "me": me, "progress": progress,
             "pool": pool, "is_practice": pool == POOL_PRACTICE,
             "peers": _peer_progress(db, int(me.id)),
             "answers": _recent_answers(db, int(me.id), pool)},
        )

    image, key = question
    return tpl().TemplateResponse(
        "measure.html",
        {
            "request": request, "me": me,
            "question": _question_payload(image, key),
            "progress": progress,
            "pool": pool, "is_practice": pool == POOL_PRACTICE,
            "peers": _peer_progress(db, int(me.id)),
            "answers": _recent_answers(db, int(me.id), pool),
            "list_limit": MEASURE_LIST_LIMIT,
            # 이력에서 넘어온 경우에만 값이 있다
            "review_id": review,
            "idle_lock_ms": settings_store.idle_lock_ms(db),
            "idle_phase1_ms": settings_store.idle_phase1_ms(db),
        },
    )


@router.post("/measure/mode/{pool}")
def measure_mode(request: Request, pool: str):
    """연습 ↔ 실제 계측 전환. 세션에만 기록하고 계측 화면으로 돌려보낸다."""
    request.session[SESSION_POOL_KEY] = (
        POOL_PRACTICE if pool == POOL_PRACTICE else POOL_REAL
    )
    return RedirectResponse("/measure", 303)


@router.post("/measure/practice/clear")
def measure_practice_clear(request: Request, db: DbSession, me: ReviewerUser):
    """내 연습 기록만 전부 삭제 — 같은 연습 영상을 처음부터 다시 잴 수 있게.

    실제 계측 pool은 절대 건드리지 않는다.
    """
    rows = db.scalars(
        select(Measurement)
        .join(Image, Image.id == Measurement.image_id)
        .where(Measurement.user_id == int(me.id), Image.pool == POOL_PRACTICE)
    ).all()
    for m in rows:
        db.delete(m)
    db.commit()
    request.session[SESSION_POOL_KEY] = POOL_PRACTICE
    return RedirectResponse("/measure", 303)


@router.get("/measure/next")
def measure_next(request: Request, db: DbSession, me: ReviewerUser):
    """다음 문제를 JSON으로. 화면 전환 없이 이어서 출제한다.

    다음 문제의 이미지는 클라이언트가 미리 받아 두었다가 '사진 공개' 순간에
    즉시 그린다 — 네트워크 대기가 소요시간에 섞이면 속도 지표가 망가진다.
    """
    pool = current_pool(request)
    q = _next_question(db, int(me.id), pool)
    return JSONResponse({
        "ok": True,
        "question": _question_payload(*q) if q else None,
        "progress": _progress(db, int(me.id), pool),
        "answers": _recent_answers(db, int(me.id), pool),
        # 계측자 현황도 매 응답에 실어 보낸다 — 문제가 넘어갈 때마다 갱신돼야
        # 한다. 첫 렌더 값에 굳어 있으면 동료 진행이 멈춘 것처럼 보인다.
        "peers": _peer_progress(db, int(me.id)),
        "done": q is None,
    })


@router.post("/measure/answer")
def measure_answer(
    db: DbSession,
    me: ReviewerUser,
    payload: Annotated[dict, Body()],
):
    """한 문제의 답 저장.

    payload = {image_id, variable_key, points: [[x,y],[x,y]], duration_ms}
    좌표는 DICOM 원본 픽셀 기준(클라이언트가 환산해 보낸다).
    """
    try:
        image_id = int(payload.get("image_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "image_id가 필요합니다.")

    key = str(payload.get("variable_key") or "")
    if not is_valid_variable(key):
        raise HTTPException(400, "알 수 없는 계측 변수입니다.")

    image = db.get(Image, image_id)
    if image is None or image.status != "active":
        raise HTTPException(404, "영상을 찾을 수 없습니다.")
    # 세션의 모드가 아니라 **영상 자신**이 pool을 정한다. 답을 보내는 사이에
    # 모드를 바꿔도 이 답이 엉뚱한 pool의 목록에 섞이지 않는다.
    pool = image.pool

    pts = normalize_points(payload.get("points"))
    if len(pts) < 2:
        raise HTTPException(400, "두 점이 필요합니다.")

    cap = settings_store.max_duration_ms(db)
    raw = payload.get("duration_ms")
    try:
        duration = max(0, min(int(raw), cap)) if raw is not None else None
    except (TypeError, ValueError):
        duration = None

    # 같은 (영상, 변수)를 이미 유효하게 답했으면 덮어쓰지 않는다 —
    # 중복 제출(뒤로가기, 더블클릭)이 기록을 흔들지 못하게.
    existing = db.scalar(
        select(Measurement).where(
            Measurement.image_id == image_id,
            Measurement.user_id == me.id,
            Measurement.variable_key == key,
            Measurement.status == M_DONE,
        )
    )
    if existing is not None:
        return JSONResponse(
            {"ok": False, "error": "already_answered",
             "message": "이미 답한 문제입니다."},
            status_code=409,
        )

    n_prev = db.scalar(
        select(func.count()).select_from(Measurement).where(
            Measurement.image_id == image_id,
            Measurement.user_id == me.id,
            Measurement.variable_key == key,
        )
    ) or 0

    row = Measurement(
        image_id=image_id,
        user_id=int(me.id),
        variable_key=key,
        points=pts,
        value_mm=value_mm(pts, image.pixel_spacing_mm),
        duration_ms=duration,
        attempt=int(n_prev) + 1,
        status=M_DONE,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    return JSONResponse({
        "ok": True,
        "measurement_id": int(row.id),
        "value_mm": None if row.value_mm is None else round(row.value_mm, 1),
        "progress": _progress(db, int(me.id), pool),
        "answers": _recent_answers(db, int(me.id), pool),
        # 계측자 현황도 매 응답에 실어 보낸다 — 문제가 넘어갈 때마다 갱신돼야
        # 한다. 첫 렌더 값에 굳어 있으면 동료 진행이 멈춘 것처럼 보인다.
        "peers": _peer_progress(db, int(me.id)),
    })


@router.post("/measure/redo")
def measure_redo(
    db: DbSession,
    me: ReviewerUser,
    payload: Annotated[dict, Body()],
):
    """재측정 요청 — 해당 계측을 무효화하고 대기 pool로 돌려보낸다.

    버려진 시도도 지우지 않고 `status='redo'`로 남긴다. 몇 번 만에 만족했는지,
    버린 시도가 얼마나 걸렸는지가 그 자체로 자료다(집계에서는 빠진다).
    """
    try:
        measurement_id = int(payload.get("measurement_id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "measurement_id가 필요합니다.")

    row = db.get(Measurement, measurement_id)
    _img = None if row is None else db.get(Image, row.image_id)
    pool = _img.pool if _img is not None else POOL_REAL
    if row is None or row.user_id != me.id:
        raise HTTPException(404, "계측을 찾을 수 없습니다.")
    if row.status != M_DONE:
        raise HTTPException(409, "이미 재측정 요청된 계측입니다.")

    row.status = M_REDO
    row.updated_at = datetime.utcnow()
    db.commit()

    v = VARIABLES.get(row.variable_key)
    return JSONResponse({
        "ok": True,
        "message": f"{v.display if v else row.variable_key} 을(를) 대기 목록으로 되돌렸습니다.",
        "progress": _progress(db, int(me.id), pool),
        "answers": _recent_answers(db, int(me.id), pool),
        # 계측자 현황도 매 응답에 실어 보낸다 — 문제가 넘어갈 때마다 갱신돼야
        # 한다. 첫 렌더 값에 굳어 있으면 동료 진행이 멈춘 것처럼 보인다.
        "peers": _peer_progress(db, int(me.id)),
    })


@router.get("/measure/review/{measurement_id}")
def measure_review(db: DbSession, me: ReviewerUser, measurement_id: int):
    """완료 목록에서 하나를 다시 열어 볼 때 필요한 정보."""
    row = db.get(Measurement, measurement_id)
    if row is None or row.user_id != me.id:
        raise HTTPException(404, "계측을 찾을 수 없습니다.")
    image = db.get(Image, row.image_id)
    if image is None:
        raise HTTPException(404, "영상을 찾을 수 없습니다.")
    v = VARIABLES.get(row.variable_key)
    return JSONResponse({
        "ok": True,
        "measurement_id": int(row.id),
        "status": row.status,
        "points": row.points or [],
        "value_mm": None if row.value_mm is None else round(row.value_mm, 1),
        "duration_ms": row.duration_ms,
        "attempt": row.attempt,
        "variable": {
            "key": row.variable_key,
            "display": v.display if v else row.variable_key,
            "short": v.short if v else row.variable_key,
            "instruction": v.instruction if v else "",
            "guide": v.guide if v else "Rt_none.png",
            "paperEn": v.paper_en if v else "",
            "pointA": v.point_a if v else "",
            "pointB": v.point_b if v else "",
            "caution": v.caution if v else "",
            "axis": v.axis if v else "oblique",
            "axisNote": axis_note(v.axis) if v else "",
        },
        "image": {
            "id": int(image.id),
            "filename": image.original_filename,
            "origW": image.orig_w,
            "origH": image.orig_h,
            "renderScale": image.render_scale,
            "pixelSpacingMm": image.pixel_spacing_mm,
            "pngUrl": f"/image/{image.id}/png",
        },
    })


@router.get("/measure/paused", response_class=HTMLResponse)
def measure_paused(request: Request, db: DbSession, me: ReviewerUser):
    """'확정 후 평가 종료'로 빠져나온 화면.

    따로 저장하는 세션 상태는 없다 — 대기 pool이 '아직 답하지 않은 쌍'으로
    계산되므로, 다시 들어오면 남은 문제부터 자연히 이어진다.
    """
    pool = current_pool(request)
    rows = db.scalars(
        select(Measurement)
        .join(Image, Image.id == Measurement.image_id)
        .where(
            Measurement.user_id == me.id,
            Measurement.status == M_DONE,
            Image.pool == pool,
        )
    ).all()
    durations = sorted(m.duration_ms for m in rows if m.duration_ms)
    median = durations[len(durations) // 2] if durations else None

    return tpl().TemplateResponse(
        "measure_paused.html",
        {"request": request, "me": me,
         "progress": _progress(db, int(me.id), pool),
         "pool": pool, "is_practice": pool == POOL_PRACTICE,
         "n_done": len(rows),
         "median_ms": median,
         "answers": _recent_answers(db, int(me.id), pool, limit=12)},
    )


# ----------------------------------------------------------------------------
# 이력
# ----------------------------------------------------------------------------


@router.get("/history", response_class=HTMLResponse)
def history(request: Request, db: DbSession, me: ReviewerUser):
    """내 계측 이력 — **지금 선택된 모드의 pool만** 보여준다.

    연습과 실제 계측을 한 표에 섞으면 "내가 실제로 몇 개나 했나"를 알 수 없고,
    소요시간 중앙값도 연습이 섞여 실제보다 느리게 나온다. 계측 화면·현황과
    같은 기준으로 맞춘다.
    """
    pool = current_pool(request)
    rows = db.scalars(
        select(Measurement)
        .join(Image, Image.id == Measurement.image_id)
        .where(Measurement.user_id == me.id, Image.pool == pool)
        .order_by(Measurement.created_at.desc(), Measurement.id.desc())
    ).all()

    items = []
    for m in rows:
        img = db.get(Image, m.image_id)
        v = VARIABLES.get(m.variable_key)
        items.append({
            "m": m,
            "filename": img.original_filename if img else "",
            "display": v.display if v else m.variable_key,
            "short": v.short if v else m.variable_key,
            "at": (
                m.created_at.replace(tzinfo=timezone.utc).astimezone(KST)
                if m.created_at else None
            ),
        })

    n_done = sum(1 for i in items if i["m"].status == M_DONE)
    n_redo = sum(1 for i in items if i["m"].status == M_REDO)
    durations = [
        i["m"].duration_ms for i in items
        if i["m"].status == M_DONE and i["m"].duration_ms
    ]
    median = None
    if durations:
        s = sorted(durations)
        median = s[len(s) // 2]

    return tpl().TemplateResponse(
        "history.html",
        {"request": request, "me": me, "items": items,
         "n_done": n_done, "n_redo": n_redo, "median_ms": median,
         "pool": pool, "is_practice": pool == POOL_PRACTICE,
         "progress": _progress(db, int(me.id), pool)},
    )


# ----------------------------------------------------------------------------
# 영상 전송 — data/ 아래 파일은 정적 마운트하지 않고 인증을 거쳐 내보낸다
# ----------------------------------------------------------------------------


def _serve_png(db: DbSession, image_id: int, enhanced: bool) -> FileResponse:
    image = db.get(Image, image_id)
    if image is None:
        raise HTTPException(404, "영상을 찾을 수 없습니다.")

    if enhanced:
        if image.png_enh_path:
            path = DATA_DIR / image.png_enh_path
            if path.exists():
                return FileResponse(path, media_type="image/png")
        base = DATA_DIR / image.png_path
        if not base.exists():
            raise HTTPException(404, "렌더 이미지가 없습니다.")
        rel = Path(image.png_path)
        out_rel = rel.with_name(rel.stem + "_enh.png")
        try:
            enhance_png(base, DATA_DIR / out_rel)
        except DicomError as exc:
            raise HTTPException(500, str(exc)) from exc
        image.png_enh_path = str(out_rel).replace("\\", "/")
        db.commit()
        return FileResponse(DATA_DIR / out_rel, media_type="image/png")

    path = DATA_DIR / image.png_path
    if not path.exists():
        raise HTTPException(404, "렌더 이미지가 없습니다.")
    # 미리 받아 두는 이미지라 브라우저 캐시를 허용한다 — 공개 순간의 지연을 없애려면
    # 캐시가 있어야 한다.
    return FileResponse(
        path, media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/image/{image_id}/png")
def image_png(
    db: DbSession,
    me: ReviewerUser,
    image_id: int,
    enhanced: Annotated[int, Query()] = 0,
):
    return _serve_png(db, image_id, bool(enhanced))
