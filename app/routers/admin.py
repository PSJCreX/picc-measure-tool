"""관리자 화면 — 현황 · 업로드 · 사용자 · 매트릭스 · 불일치/GT · 설정 · 내보내기."""
from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from pathlib import Path

from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app import config
from app import export as export_mod
from app import settings_store
from app.agreement import (
    KIND_LABELS,
    KIND_MISMATCH,
    active_reviewers,
    classify_all,
    classify_image,
    current_measurements,
    summarize,
)
from app import reset as reset_mod
from app.auth import generate_temp_password, hash_password, verify_password
from app.config import DATA_DIR, DEFAULT_SETTINGS
from app.deps import AdminUser, DbSession, MatrixViewer
from app.dicom_io import decoder_status
from app.geometry import value_mm as compute_value_mm
from app.ingest import ingest_upload
from app.models import (
    M_DONE,
    Prediction,
    POOL_LABELS,
    POOL_PRACTICE,
    POOL_REAL,
    ROLE_ADMIN,
    ROLE_REVIEWER,
    SPACING_MANUAL,
    Image,
    Measurement,
    User,
)
from app.variables import ALL_VARIABLE_KEYS, N_VARIABLES, VARIABLES

router = APIRouter(prefix="/admin")
_templates: Jinja2Templates | None = None


def set_templates(t: Jinja2Templates) -> None:
    global _templates
    _templates = t


def tpl() -> Jinja2Templates:
    assert _templates is not None
    return _templates


def _flash(request: Request, message: str, kind: str = "flash") -> None:
    """다음 화면에 한 번만 보여줄 메시지를 세션에 넣는다.

    쿼리스트링을 쓰지 않는 이유: 계정 임시 비밀번호가 여기 실려 나가는데
    URL은 브라우저 방문기록과 서버 액세스 로그에 그대로 남는다.
    """
    request.session[kind] = message


def _pop_flashes(request: Request) -> tuple[str | None, str | None]:
    return request.session.pop("flash", None), request.session.pop("error", None)


def _render(request: Request, name: str, me: User, active: str, **ctx) -> HTMLResponse:
    base: dict[str, Any] = {
        "request": request,
        "me": me,
        "active": active,
        "VARIABLES": VARIABLES,
        "VARIABLE_KEYS": ALL_VARIABLE_KEYS,
        "N_VARIABLES": N_VARIABLES,
        "KIND_LABELS": KIND_LABELS,
    }
    base.update(ctx)
    return tpl().TemplateResponse(name, base)


# ----------------------------------------------------------------------------
# 현황
# ----------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: DbSession, me: AdminUser):
    tol = settings_store.tolerance_mm(db)

    images = list(
        db.scalars(
            select(Image).where(Image.status == "active", Image.pool == POOL_REAL)
        ).all()
    )
    reviewers = active_reviewers(db)
    agreements = classify_all(db, images, tol)
    counts = summarize(agreements)
    n_images = len(images)

    # 변수별 진행 — 모든 영상이 8개 변수 전부의 대상이다
    per_variable = []
    for key in ALL_VARIABLE_KEYS:
        # ⚠️ 반드시 pool 로 걸러야 한다. 분모(n_images)는 실제 계측 pool 기준인데
        # 분자에 연습 계측이 섞이면 진행률이 100%를 넘고 조용히 틀린다.
        n_done = db.scalar(
            select(func.count()).select_from(Measurement)
            .join(Image, Image.id == Measurement.image_id)
            .where(
                Measurement.variable_key == key,
                Measurement.status == M_DONE,
                Image.pool == POOL_REAL,
            )
        ) or 0
        target = n_images * len(reviewers)
        per_variable.append({
            "key": key,
            "display": VARIABLES[key].display,
            "done": int(n_done),
            "target": target,
            "pct": round(100.0 * n_done / target, 1) if target else 0.0,
        })

    # 계측자별 진행 — 분모는 영상 수 × 8
    total_per_reviewer = n_images * N_VARIABLES
    per_reviewer = []
    for u in reviewers:
        done = db.scalar(
            select(func.count()).select_from(Measurement)
            .join(Image, Image.id == Measurement.image_id)
            .where(
                Measurement.user_id == u.id,
                Measurement.status == M_DONE,
                Image.pool == POOL_REAL,
            )
        ) or 0
        redo = db.scalar(
            select(func.count()).select_from(Measurement)
            .join(Image, Image.id == Measurement.image_id)
            .where(
                Measurement.user_id == u.id,
                Measurement.status != M_DONE,
                Image.pool == POOL_REAL,
            )
        ) or 0
        # 소요시간 중앙값은 가설 2(속도)의 숫자다. 연습이 섞이면 안 된다 —
        # 연습은 손에 익히는 과정이라 느리고, 그게 본 계측 속도로 보고된다.
        durations = sorted(
            d for (d,) in db.execute(
                select(Measurement.duration_ms)
                .join(Image, Image.id == Measurement.image_id)
                .where(
                    Measurement.user_id == u.id,
                    Measurement.status == M_DONE,
                    Measurement.duration_ms.is_not(None),
                    Image.pool == POOL_REAL,
                )
            ).all() if d
        )
        per_reviewer.append({
            "name": u.display_label(),
            "username": u.username,
            "done": int(done),
            "redo": int(redo),
            "total": total_per_reviewer,
            "pct": round(100.0 * done / total_per_reviewer, 1)
                   if total_per_reviewer else 0.0,
            "median_ms": durations[len(durations) // 2] if durations else None,
        })
    per_reviewer.sort(key=lambda r: -r["done"])

    no_spacing = [im for im in images if not im.has_spacing]

    # 연습 pool 은 집계에서 빠지지만, **존재 사실은 보여야 한다.**
    # 안 보이면 연습용으로 올린 영상이 어디 갔는지 알 길이 없다.
    n_practice_images = int(db.scalar(
        select(func.count()).select_from(Image)
        .where(Image.status == "active", Image.pool == POOL_PRACTICE)
    ) or 0)
    n_practice_measurements = int(db.scalar(
        select(func.count()).select_from(Measurement)
        .join(Image, Image.id == Measurement.image_id)
        .where(Measurement.status == M_DONE, Image.pool == POOL_PRACTICE)
    ) or 0)

    return _render(
        request, "admin_dashboard.html", me, "dashboard",
        n_images=n_images,
        n_practice_images=n_practice_images,
        n_practice_measurements=n_practice_measurements,
        n_reviewers=len(reviewers),
        n_questions=n_images * N_VARIABLES,
        counts=counts,
        per_variable=per_variable,
        per_reviewer=per_reviewer,
        tolerance_mm=tol,
        no_spacing=no_spacing[:50],
        n_no_spacing=len(no_spacing),
        decoders=decoder_status(),
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )


# ----------------------------------------------------------------------------
# 업로드
# ----------------------------------------------------------------------------


@router.get("/upload", response_class=HTMLResponse)
def upload_page(request: Request, db: DbSession, me: AdminUser):
    counts = {
        p: int(db.scalar(
            select(func.count()).select_from(Image)
            .where(Image.status == "active", Image.pool == p)
        ) or 0)
        for p in (POOL_REAL, POOL_PRACTICE)
    }
    return _render(
        request, "admin_upload.html", me, "upload",
        render_max_px=settings_store.render_max_px(db),
        decoders=decoder_status(),
        pool_counts=counts,
        pool_labels=POOL_LABELS,
        POOL_REAL=POOL_REAL,
        POOL_PRACTICE=POOL_PRACTICE,
    )


@router.post("/upload")
async def upload_post(
    db: DbSession,
    me: AdminUser,
    files: Annotated[list[UploadFile], File()],
    pool: Annotated[str | None, Form()] = None,
):
    """파일 여러 개를 한 번에 받는다.

    브라우저는 XHR로 몇 개씩 끊어 올리고, 응답으로 파일별 결과를 받아
    진행률 목록에 반영한다. 등재된 영상은 자동으로 8개 항목 전부의 대상이 된다.

    `pool` 은 이 영상들을 **연습용**으로 넣을지 **실제 계측용**으로 넣을지다.
    연습 영상과 그 계측은 어떤 내보내기·집계에도 들어가지 않는다.

    **기본값을 두지 않는다.** 예전에는 값이 없으면 조용히 'real' 로 처리했는데,
    브라우저가 옛 upload.js 를 캐시하고 있던 탓에 pool 필드가 아예 오지 않았고,
    연습용으로 올린 영상이 전부 실제 계측 pool 로 들어갔다. 화면에는 아무 이상도
    보이지 않았다. 값이 없으면 **거부하고 이유를 말하는 편**이 훨씬 안전하다.
    """
    if pool not in (POOL_REAL, POOL_PRACTICE):
        raise HTTPException(
            400,
            "업로드 대상(연습/실제 계측)이 전달되지 않았습니다. "
            "브라우저가 오래된 스크립트를 쓰고 있을 수 있습니다 — "
            "Ctrl+F5 로 새로고침한 뒤 다시 시도하세요.",
        )
    target_pool = pool
    max_px = settings_store.render_max_px(db)
    results: list[dict] = []
    for f in files:
        content = await f.read()
        results.extend(
            ingest_upload(
                db,
                filename=f.filename or "unnamed.dcm",
                content=content,
                uploaded_by=int(me.id),
                render_max_px=max_px,
                pool=target_pool,
            )
        )
    return JSONResponse({"ok": True, "results": results, "pool": target_pool})


# ----------------------------------------------------------------------------
# 사용자
# ----------------------------------------------------------------------------


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: DbSession, me: AdminUser):
    flash, error = _pop_flashes(request)
    users = list(db.scalars(select(User).order_by(User.role, User.username)).all())
    # 화면에 보이는 건수는 **실제 계측만** — 연습이 섞이면 누가 얼마나 했는지
    # 잘못 읽는다. (계정 삭제 가드는 연습까지 포함한 전체를 따로 센다 —
    # 연습 기록이라도 남아 있으면 지우지 않는 편이 안전하다.)
    counts = {
        uid: int(n) for uid, n in db.execute(
            select(Measurement.user_id, func.count())
            .join(Image, Image.id == Measurement.image_id)
            .where(Measurement.status == M_DONE, Image.pool == POOL_REAL)
            .group_by(Measurement.user_id)
        ).all()
    }
    return _render(
        request, "admin_users.html", me, "users",
        users=users, counts=counts, flash=flash, error=error,
    )


@router.post("/users/create")
def users_create(
    request: Request,
    db: DbSession,
    me: AdminUser,
    username: Annotated[str, Form()],
    full_name: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = ROLE_REVIEWER,
):
    uname = username.strip()
    if not uname:
        _flash(request, "아이디를 입력하세요.", "error")
        return RedirectResponse("/admin/users", 303)
    if db.scalar(select(User).where(User.username == uname)):
        _flash(request, "이미 있는 아이디입니다.", "error")
        return RedirectResponse("/admin/users", 303)
    if role not in (ROLE_ADMIN, ROLE_REVIEWER):
        role = ROLE_REVIEWER

    temp = generate_temp_password()
    db.add(User(
        username=uname,
        full_name=full_name.strip(),
        password_hash=hash_password(temp),
        role=role,
        is_active=True,
        must_change_password=True,
    ))
    db.commit()
    _flash(request, f"{uname} 계정을 만들었습니다. 임시 비밀번호: {temp}")
    return RedirectResponse("/admin/users", 303)


@router.post("/users/{user_id}/password")
def users_reset_password(request: Request, db: DbSession, me: AdminUser, user_id: int):
    u = db.get(User, user_id)
    if not u:
        _flash(request, "계정을 찾을 수 없습니다.", "error")
        return RedirectResponse("/admin/users", 303)
    temp = generate_temp_password()
    u.password_hash = hash_password(temp)
    u.must_change_password = True
    db.commit()
    _flash(request, f"{u.username} 새 임시 비밀번호: {temp}")
    return RedirectResponse("/admin/users", 303)


@router.post("/users/{user_id}/toggle")
def users_toggle(request: Request, db: DbSession, me: AdminUser, user_id: int):
    u = db.get(User, user_id)
    if not u:
        _flash(request, "계정을 찾을 수 없습니다.", "error")
        return RedirectResponse("/admin/users", 303)
    if u.id == me.id:
        _flash(request, "자기 계정은 비활성화할 수 없습니다.", "error")
        return RedirectResponse("/admin/users", 303)
    u.is_active = not u.is_active
    db.commit()
    _flash(request, f"{u.username} 계정을 {'활성화' if u.is_active else '비활성화'}했습니다.")
    return RedirectResponse("/admin/users", 303)


@router.post("/users/{user_id}/role")
def users_set_role(
    request: Request, db: DbSession, me: AdminUser, user_id: int,
    role: Annotated[str, Form()],
):
    u = db.get(User, user_id)
    if not u:
        _flash(request, "계정을 찾을 수 없습니다.", "error")
        return RedirectResponse("/admin/users", 303)
    if u.id == me.id:
        _flash(request, "자기 역할은 바꿀 수 없습니다.", "error")
        return RedirectResponse("/admin/users", 303)
    if role not in (ROLE_ADMIN, ROLE_REVIEWER):
        _flash(request, "알 수 없는 역할입니다.", "error")
        return RedirectResponse("/admin/users", 303)
    u.role = role
    db.commit()
    _flash(request, f"{u.username} 역할을 변경했습니다.")
    return RedirectResponse("/admin/users", 303)


@router.post("/users/{user_id}/name")
def users_set_name(
    request: Request, db: DbSession, me: AdminUser, user_id: int,
    full_name: Annotated[str, Form()],
):
    u = db.get(User, user_id)
    if not u:
        _flash(request, "계정을 찾을 수 없습니다.", "error")
        return RedirectResponse("/admin/users", 303)
    u.full_name = full_name.strip()
    db.commit()
    _flash(request, "이름을 변경했습니다.")
    return RedirectResponse("/admin/users", 303)


@router.post("/users/{user_id}/delete")
def users_delete(
    request: Request, db: DbSession, me: AdminUser, user_id: int,
    confirm: Annotated[str, Form()] = "",
):
    u = db.get(User, user_id)
    if not u:
        _flash(request, "계정을 찾을 수 없습니다.", "error")
        return RedirectResponse("/admin/users", 303)
    if u.id == me.id:
        _flash(request, "자기 계정은 삭제할 수 없습니다.", "error")
        return RedirectResponse("/admin/users", 303)
    if confirm.strip() != u.username:
        _flash(request, "확인 문구가 다릅니다.", "error")
        return RedirectResponse("/admin/users", 303)

    # 계측 기록이 있으면 삭제 대신 비활성화를 권한다 — 연구 데이터 소실 방지
    n = db.scalar(
        select(func.count()).select_from(Measurement).where(Measurement.user_id == u.id)
    ) or 0
    if n:
        _flash(request, f"{u.username}에 계측 기록 {n}건이 있습니다. 비활성화를 사용하세요.", "error")
        return RedirectResponse("/admin/users", 303)
    db.delete(u)
    db.commit()
    _flash(request, "계정을 삭제했습니다.")
    return RedirectResponse("/admin/users", 303)


# ----------------------------------------------------------------------------
# 매트릭스
# ----------------------------------------------------------------------------


def _matrix_context(
    db: DbSession, only_mismatch: bool, focus_id: int | None
) -> dict[str, Any]:
    tol = settings_store.tolerance_mm(db)

    images = list(
        db.scalars(
            select(Image)
            .where(Image.status == "active", Image.pool == POOL_REAL)
            .order_by(Image.id)
        ).all()
    )
    reviewers = active_reviewers(db)
    agreements = classify_all(db, images, tol)
    per_image = current_measurements(db, [im.id for im in images])

    def build_row(im: Image) -> dict[str, Any]:
        agr = agreements.get(im.id)
        cells = []
        for u in reviewers:
            mine = per_image.get(im.id, {}).get(int(u.id), {})
            values = []
            for k in ALL_VARIABLE_KEYS:
                m = mine.get(k)
                va = agr.per_variable.get(k) if agr else None
                values.append({
                    "key": k,
                    "short": VARIABLES[k].short,
                    "mm": None if not m or m.value_mm is None else round(m.value_mm, 1),
                    "ms": m.duration_ms if m else None,
                    "mismatch": bool(va and va.kind == KIND_MISMATCH),
                })
            n_mine = sum(1 for v in values if v["mm"] is not None)
            cells.append({
                "user": u,
                "n_done": n_mine,
                # 키 이름을 'values'로 두면 Jinja가 dict.values 메서드로 해석한다
                "vals": values,
            })
        return {
            "image": im,
            "kind": agr.kind if agr else "empty",
            "agreement": agr,
            # 'keys'로 두면 Jinja가 dict.keys 메서드로 해석한다
            "var_keys": ALL_VARIABLE_KEYS,
            "cells": cells,
            "n_measured": agr.n_measured if agr else 0,
            "n_expected": agr.n_expected if agr else 0,
            # 확정값 대신 '비교 가능한 항목이 몇 개인가'를 센다
            "n_paired": sum(
                1 for k in ALL_VARIABLE_KEYS
                if agr and len(agr.per_variable[k].values) >= 2
            ) if agr else 0,
        }

    rows = [
        build_row(im) for im in images
        if not (only_mismatch and agreements[im.id].kind != KIND_MISMATCH)
    ]

    focus = None
    if focus_id is not None:
        focus = next((r for r in rows if r["image"].id == focus_id), None)
        if focus is None:
            im = db.get(Image, focus_id)
            if im is not None and im.id in agreements:
                focus = build_row(im)

    return {
        "rows": rows,
        "reviewers": reviewers,
        "tolerance_mm": tol,
        "focus": focus,
        "only_mismatch": only_mismatch,
        "counts": summarize(agreements),
    }


# 계측자 구분 색. 영상 위에 여러 사람의 선을 겹쳐 그리므로 색으로만 구분된다.
# 배경(흉부 X-ray, 회색조) 위에서 서로 구분되고 색각 이상에서도 갈리는 조합으로 골랐다.
REVIEWER_COLORS = [
    "#e6194b",  # 적
    "#3cb44b",  # 녹
    "#4363d8",  # 청
    "#f58231",  # 주
    "#911eb4",  # 자
    "#46f0f0",  # 청록
    "#f032e6",  # 분홍
    "#bcf60c",  # 연두
]


def _neighbor_ids(
    db: DbSession, image_id: int, only_mismatch: bool, tol: float
) -> tuple[int | None, int | None, int, int]:
    """이전·다음 영상 id 와 (현재 순번, 전체 개수).

    **들어온 목록과 같은 범위**에서 이웃을 찾는다 — 불일치 화면에서 들어왔으면
    불일치 영상들 사이를 오간다. 전체 목록을 쓰면 '불일치만 보려고 들어왔는데
    멀쩡한 영상으로 넘어가는' 일이 생긴다.
    """
    images = list(
        db.scalars(
            select(Image)
            .where(Image.status == "active", Image.pool == POOL_REAL)
            .order_by(Image.id)
        ).all()
    )
    if only_mismatch:
        agrs = classify_all(db, images, tol)
        images = [im for im in images if agrs[im.id].kind == KIND_MISMATCH]
    ids = [int(im.id) for im in images]
    if image_id not in ids:
        return None, None, 0, len(ids)
    i = ids.index(image_id)
    return (ids[i - 1] if i > 0 else None,
            ids[i + 1] if i + 1 < len(ids) else None,
            i + 1, len(ids))


def _image_detail_context(
    db: DbSession, image_id: int, back: str, me: User | None = None
) -> dict[str, Any]:
    """영상 한 장의 상세 — 계측자들이 그린 선을 영상 위에 겹쳐 보기 위한 자료.

    좌표는 DB 에 **DICOM 원본 픽셀**로 저장돼 있다. 화면에 띄우는 PNG 는 렌더
    해상도로 줄어 있으므로(`Image.render_scale`), 클라이언트가
    `원본 px × render_scale = PNG px` 로 환산해 그린다. 여기서는 환산하지 않고
    원본 좌표와 배율을 그대로 넘긴다 — 환산을 한 곳에서만 하면 어긋날 일이 없다.
    """
    image = db.get(Image, image_id)
    if image is None:
        raise HTTPException(404, "영상을 찾을 수 없습니다.")

    tol = settings_store.tolerance_mm(db)
    reviewers = active_reviewers(db)
    per_user = current_measurements(db, [image_id]).get(image_id, {})
    agr = classify_image(image, reviewers, per_user, tol)

    # 계측자별 — 색과 함께. 계측이 하나도 없는 사람도 목록에는 남긴다
    # (토글에서 '이 사람은 아직 안 쟀다'가 보여야 한다).
    raters = []
    for idx, u in enumerate(reviewers):
        mine = per_user.get(int(u.id), {})
        # 키 이름을 'items' 로 두면 Jinja 가 **dict.items 메서드**로 해석한다.
        # (이 프로젝트에서 'values'·'keys' 로 이미 두 번 당했다 — §11 참고)
        lines = []
        for key in ALL_VARIABLE_KEYS:
            m = mine.get(key)
            if m is None or not m.points:
                continue
            lines.append({
                "key": key,
                "short": VARIABLES[key].short,
                "display": VARIABLES[key].display,
                "points": m.points,          # DICOM 원본 px
                "value_mm": None if m.value_mm is None else round(m.value_mm, 1),
                "duration_ms": m.duration_ms,
                "attempt": m.attempt,
            })
        raters.append({
            "id": int(u.id),
            "name": u.display_label(),
            "username": u.username,
            "color": REVIEWER_COLORS[idx % len(REVIEWER_COLORS)],
            "n": len(lines),
            "lines": lines,
        })

    # 항목별 판정 — 어느 항목이 벌어졌는지 상세에서도 보여야 한다
    per_variable = []
    for key in ALL_VARIABLE_KEYS:
        va = agr.per_variable.get(key)
        per_variable.append({
            "key": key,
            "short": VARIABLES[key].short,
            "display": VARIABLES[key].display,
            "kind": va.kind if va else "empty",
            "spread_mm": round(va.spread_mm, 1) if va and va.spread_mm is not None else None,
            "n": len(va.values) if va else 0,
        })

    # 소요시간 표 — 가설 2(속도)의 사람 쪽 숫자.
    #
    # `duration_ms` 는 '사진이 뜬 순간 ~ 마지막 drop' 구간이다. 문제를 읽는 시간과
    # 확정을 고민한 시간은 들어 있지 않다 — AI 추론 시간과 맞댈 수 있는 구간만
    # 남긴 값이다. 모델 쪽 열은 예측이 들어오면 채워진다(아직 없음).
    times = []
    for key in ALL_VARIABLE_KEYS:
        cells = []
        for r in raters:
            ln = next((x for x in r["lines"] if x["key"] == key), None)
            cells.append(ln["duration_ms"] if ln else None)
        times.append({
            "key": key,
            "short": VARIABLES[key].short,
            "display": VARIABLES[key].display,
            "cells": cells,
        })

    def _summary(vals: list[int | None]) -> dict[str, Any]:
        got = sorted(v for v in vals if v)
        return {
            "n": len(got),
            "total_ms": sum(got) if got else None,
            "median_ms": got[len(got) // 2] if got else None,
        }

    time_summary = [
        _summary([t["cells"][i] for t in times]) for i in range(len(raters))
    ]

    prev_id, next_id, pos, total = _neighbor_ids(
        db, image_id, back.endswith("disagreements"), tol
    )

    return {
        "image": image,
        "prev_id": prev_id,
        "next_id": next_id,
        "pos": pos,
        "total": total,
        "raters": raters,
        "per_variable": per_variable,
        "times": times,
        "time_summary": time_summary,
        "agreement": agr,
        "tolerance_mm": tol,
        "back": back,
        "png_url": f"/admin/image/{image.id}/png",
        "png_enh_url": f"/admin/image/{image.id}/png?enhanced=1",
    }


@router.get("/matrix/image/{image_id}", response_class=HTMLResponse)
def matrix_image_detail(
    request: Request, db: DbSession, me: MatrixViewer, image_id: int,
    back: Annotated[str, Query()] = "/admin/matrix",
):
    """계측 매트릭스에서 한 행을 눌러 들어오는 하위 화면.

    표가 있던 자리에 영상을 띄우고 계측자들의 선을 겹쳐 보여준다. 좁은 우측
    패널에 숫자만 띄우던 예전 방식으로는 '누가 어디를 재서 벌어졌는가'를 알 수
    없었다 — 그걸 보려면 영상 위에 겹쳐 봐야 한다.
    """
    # 돌아갈 곳은 화이트리스트로 제한한다(열린 리다이렉트 방지)
    # 계측자에게는 불일치 화면이 닫혀 있으므로 거기로 돌려보내면 403 이 된다
    allowed = ("/admin/matrix", "/admin/disagreements") \
        if me.role == ROLE_ADMIN else ("/admin/matrix",)
    safe_back = back if back in allowed else "/admin/matrix"
    ctx = _image_detail_context(db, image_id, safe_back, me)
    return _render(request, "admin_image_detail.html", me,
                   "disagreements" if safe_back.endswith("disagreements") else "matrix",
                   **ctx)


# 요청 시 AI 추론 — 추론 환경은 웹과 **다른 venv** 에 있다.
#
# 웹툴은 numpy 2.x + pydicom 3.x, 추론은 torch(+CUDA 라이브러리 3GB)를 쓴다.
# 한 venv 에 몰아넣으면 서로의 의존성을 끌어내리므로 분리해 두고, 여기서는
# 하위 프로세스로 부른다. 진행 상황은 그 프로세스가 한 줄씩 찍는 것을
# **SSE 로 그대로 중계**해 화면의 progress bar 가 실제 단계를 보여준다.
# 경로는 .env 의 INFER_PYTHON / INFER_SCRIPT 로 지정한다 (config 참조).
INFER_PYTHON = config.INFER_PYTHON
INFER_SCRIPT = config.INFER_SCRIPT


@router.get("/image/{image_id}/predict")
async def image_predict_stream(db: DbSession, me: MatrixViewer, image_id: int):
    """AI 추론을 돌리며 진행을 SSE 로 흘려보낸다.

    이벤트는 추론 스크립트가 찍는 `@@P@@`(진행) / `@@R@@`(결과) 줄을 그대로
    옮긴 것이다. 검출 모듈들이 stdout 에 자기 메시지를 찍기 때문에, 그 소음과
    구분하려고 접두어를 쓴다.
    """
    image = db.get(Image, image_id)
    if image is None:
        raise HTTPException(404, "영상을 찾을 수 없습니다.")
    if not INFER_PYTHON.exists() or not INFER_SCRIPT.exists():
        raise HTTPException(
            503,
            "추론 환경이 준비되지 않았습니다 "
            f"({INFER_PYTHON} / {INFER_SCRIPT} 확인)",
        )

    async def gen():
        import asyncio
        import json as _json

        proc = await asyncio.create_subprocess_exec(
            str(INFER_PYTHON), str(INFER_SCRIPT), "--image-id", str(image_id),
            cwd=str(INFER_SCRIPT.parent.parent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("@@P@@ "):
                    yield f"event: progress\ndata: {line[6:]}\n\n"
                elif line.startswith("@@R@@ "):
                    yield f"event: result\ndata: {line[6:]}\n\n"
                # 그 밖의 줄(모듈 초기화 메시지)은 버린다
            rc = await proc.wait()
            if rc != 0:
                yield ("event: result\ndata: " +
                       _json.dumps({"ok": False,
                                    "error": f"추론 프로세스가 코드 {rc} 로 끝났습니다."})
                       + "\n\n")
        finally:
            if proc.returncode is None:
                proc.kill()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/image/{image_id}/prediction")
def image_prediction_cached(db: DbSession, me: MatrixViewer, image_id: int):
    """저장된 예측(있으면). 화면이 처음 열릴 때 이미 잰 결과가 있는지 본다."""
    rows = list(db.scalars(
        select(Prediction).where(Prediction.image_id == image_id)
    ).all())
    if not rows:
        return JSONResponse({"ok": True, "cached": False, "results": []})
    order = {k: i for i, k in enumerate(ALL_VARIABLE_KEYS)}
    rows.sort(key=lambda r: order.get(r.variable_key, 99))
    first = rows[0]
    return JSONResponse({
        "ok": True,
        "cached": True,
        "device": first.device,
        "device_name": first.device_name,
        "torch_version": first.torch_version,
        "measured_at": first.created_at.isoformat() if first.created_at else None,
        "preprocess_ms": first.preprocess_ms,
        "total_infer_ms": round(sum(r.infer_ms or 0 for r in rows), 2),
        "results": [{
            "variable_key": r.variable_key,
            "label": VARIABLES[r.variable_key].display
                     if r.variable_key in VARIABLES else r.variable_key,
            "short": VARIABLES[r.variable_key].short
                     if r.variable_key in VARIABLES else r.variable_key,
            "points": r.points,
            "value_mm": None if r.value_mm is None else round(r.value_mm, 1),
            "infer_ms": r.infer_ms,
        } for r in rows],
    })


@router.get("/matrix", response_class=HTMLResponse)
def matrix_page(
    request: Request, db: DbSession, me: MatrixViewer,
    focus: Annotated[int | None, Query()] = None,
):
    ctx = _matrix_context(db, only_mismatch=False, focus_id=focus)
    return _render(request, "admin_matrix.html", me, "matrix", **ctx)


@router.get("/disagreements", response_class=HTMLResponse)
def disagreements_page(
    request: Request, db: DbSession, me: AdminUser,
    focus: Annotated[int | None, Query()] = None,
):
    ctx = _matrix_context(db, only_mismatch=True, focus_id=focus)
    return _render(request, "admin_matrix.html", me, "disagreements", **ctx)


# ----------------------------------------------------------------------------
# 영상 관리
# ----------------------------------------------------------------------------


@router.get("/image/{image_id}/png")
def admin_image_png(
    db: DbSession, me: MatrixViewer, image_id: int,
    enhanced: Annotated[int, Query()] = 0,
):
    from app.routers.reviewer import _serve_png
    return _serve_png(db, image_id, bool(enhanced))


@router.post("/image/{image_id}/spacing")
def image_set_spacing(
    db: DbSession,
    me: AdminUser,
    image_id: int,
    pixel_spacing_mm: Annotated[str, Form()],
    redirect_to: Annotated[str, Form()] = "/admin",
):
    """스페이싱 수동 보정.

    DICOM에 PixelSpacing도 ImagerPixelSpacing도 없는 영상을 구제하는 경로다.
    값이 바뀌면 그 영상의 기존 계측값(mm)을 전부 다시 계산한다.
    """
    im = db.get(Image, image_id)
    if im is None:
        raise HTTPException(404, "영상을 찾을 수 없습니다.")
    try:
        v = float(pixel_spacing_mm)
    except (TypeError, ValueError):
        raise HTTPException(400, "숫자를 입력하세요.")
    if not (0 < v < 10):
        raise HTTPException(400, "0보다 크고 10mm 미만이어야 합니다.")

    im.pixel_spacing_mm = v
    im.spacing_source = SPACING_MANUAL

    for m in db.scalars(
        select(Measurement).where(Measurement.image_id == image_id)
    ).all():
        m.value_mm = compute_value_mm(m.points or [], v)
    db.commit()
    return RedirectResponse(redirect_to, 303)


@router.post("/image/{image_id}/remove")
def image_remove(
    db: DbSession, me: AdminUser, image_id: int,
    confirm: Annotated[str, Form()] = "",
    redirect_to: Annotated[str, Form()] = "/admin/matrix",
):
    """계측 풀에서 제외(soft delete). 계측 기록은 남긴다."""
    im = db.get(Image, image_id)
    if im is None:
        raise HTTPException(404, "영상을 찾을 수 없습니다.")
    if confirm.strip() != str(image_id):
        raise HTTPException(400, "확인 문구(영상ID)가 다릅니다.")
    im.status = "removed"
    db.commit()
    return RedirectResponse(redirect_to, 303)


# ----------------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: DbSession, me: AdminUser):
    flash, _ = _pop_flashes(request)
    images = list(
        db.scalars(
            select(Image)
            .where(Image.status == "active", Image.pixel_spacing_mm.is_(None))
            .order_by(Image.id)
        ).all()
    )
    # 공개를 켰을 때 누구에게 열리는지 미리 보여준다 — 남은 작업이 있는
    # 계측자에게는 켜도 열리지 않는다.
    from app.deps import reviewer_remaining
    gate_rows = [{
        "name": u.display_label(),
        "username": u.username,
        "remaining": reviewer_remaining(db, u),
    } for u in active_reviewers(db)]

    return _render(
        request, "admin_settings.html", me, "settings",
        settings=settings_store.all_settings(db),
        defaults=DEFAULT_SETTINGS,
        no_spacing=images,
        gate_rows=gate_rows,
        gate_open=settings_store.reviewer_review_access(db),
        flash=flash,
    )


@router.post("/settings")
def settings_save(
    request: Request,
    db: DbSession,
    me: AdminUser,
    tolerance_mm: Annotated[str, Form()],
    render_max_px: Annotated[str, Form()],
    idle_lock_ms: Annotated[str, Form()],
    idle_phase1_ms: Annotated[str, Form()],
    reviewer_review_access: Annotated[str, Form()] = "",
):
    def as_float(raw: str, lo: float, hi: float, name: str) -> float:
        try:
            v = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{name}: 숫자를 입력하세요.")
        if not (lo <= v <= hi):
            raise HTTPException(400, f"{name}: {lo}~{hi} 범위여야 합니다.")
        return v

    tol = as_float(tolerance_mm, 0.1, 200.0, "허용범위(mm)")
    px = int(as_float(render_max_px, 512, 8192, "렌더 해상도"))
    lock = int(as_float(idle_lock_ms, 5000, 3600000, "무조작 잠금"))
    ph1 = int(as_float(idle_phase1_ms, 1000, 600000, "잠금 2단계 전환"))

    settings_store.set_value(db, "tolerance_mm", str(tol))
    settings_store.set_value(db, "render_max_px", str(px))
    settings_store.set_value(db, "idle_lock_ms", str(lock))
    settings_store.set_value(db, "idle_phase1_ms", str(ph1))
    # 체크박스는 꺼져 있으면 아무 값도 오지 않는다 → 빈 문자열이면 '0'
    settings_store.set_value(
        db, "reviewer_review_access",
        "1" if reviewer_review_access.strip() in ("1", "on", "true") else "0")

    # 허용범위가 바뀌면 전 영상의 계측자 간 일치 판정이 다시 계산된다.
    # (계측값 자체는 건드리지 않는다 — 판정은 보고용 지표일 뿐이다.)
    _flash(request, "저장했습니다.")
    return RedirectResponse("/admin/settings", 303)


# ----------------------------------------------------------------------------
# 초기화 — 되돌릴 수 없으므로 비밀번호 + 확인 문구 이중 확인
# ----------------------------------------------------------------------------


@router.get("/reset", response_class=HTMLResponse)
def reset_page(request: Request, db: DbSession, me: AdminUser):
    flash, error = _pop_flashes(request)
    return _render(
        request, "admin_reset.html", me, "reset",
        scopes=list(reset_mod.SCOPES.values()),
        counts=reset_mod.preview_counts(db),
        flash=flash, error=error,
    )


@router.post("/reset")
def reset_execute(
    request: Request,
    db: DbSession,
    me: AdminUser,
    scope: Annotated[str, Form()],
    password: Annotated[str, Form()],
    confirm_phrase: Annotated[str, Form()],
):
    """테스트 데이터 초기화.

    안전장치 세 겹:
      1) 관리자 역할 (AdminUser 의존성)
      2) **본인 계정 비밀번호** 재입력 — 자리를 비운 사이 남이 누르는 것을 막는다
      3) 범위별 확인 문구 타이핑 — 무엇을 지우는지 읽게 만든다
    """
    target = reset_mod.SCOPES.get(scope)
    if target is None:
        _flash(request, "알 수 없는 초기화 범위입니다.", "error")
        return RedirectResponse("/admin/reset", 303)

    if not verify_password(password, me.password_hash):
        _flash(request, "비밀번호가 올바르지 않습니다. 초기화하지 않았습니다.", "error")
        return RedirectResponse("/admin/reset", 303)

    if (confirm_phrase or "").strip() != target.phrase:
        _flash(
            request,
            f"확인 문구가 다릅니다. '{target.phrase}' 를 정확히 입력하세요. 초기화하지 않았습니다.",
            "error",
        )
        return RedirectResponse("/admin/reset", 303)

    removed = reset_mod.execute(db, target.key)

    parts = []
    for key, label in (
        ("measurements", "계측"),
        ("images", "영상"),
        ("reviewers", "계측자 계정"), ("settings", "설정"),
    ):
        if removed.get(key):
            parts.append(f"{label} {removed[key]}건")
    detail = ", ".join(parts) if parts else "지울 데이터가 없었습니다"
    backup = removed.get("_backup") or ""
    note = f" 지우기 전 백업: {backup}" if backup else ""
    _flash(request, f"{target.title} 완료 — {detail}.{note}")
    return RedirectResponse("/admin/reset", 303)


# ----------------------------------------------------------------------------
# 내보내기
# ----------------------------------------------------------------------------


@router.get("/export", response_class=HTMLResponse)
def export_page(request: Request, db: DbSession, me: AdminUser):
    tol = settings_store.tolerance_mm(db)

    n_images = db.scalar(
        select(func.count()).select_from(Image)
        .where(Image.status == "active", Image.pool == POOL_REAL)
    ) or 0
    # 이 화면의 숫자는 전부 **내보내기에 실제로 담기는 것**이어야 한다.
    # 연습이 섞이면 "얼마나 모였나"를 잘못 읽고, 중앙값은 가설 2 지표를 오염시킨다.
    n_measurements = db.scalar(
        select(func.count()).select_from(Measurement)
        .join(Image, Image.id == Measurement.image_id)
        .where(Measurement.status == M_DONE, Image.pool == POOL_REAL)
    ) or 0
    n_redo = db.scalar(
        select(func.count()).select_from(Measurement)
        .join(Image, Image.id == Measurement.image_id)
        .where(Measurement.status != M_DONE, Image.pool == POOL_REAL)
    ) or 0
    durations = sorted(
        d for (d,) in db.execute(
            select(Measurement.duration_ms)
            .join(Image, Image.id == Measurement.image_id)
            .where(
                Measurement.status == M_DONE,
                Measurement.duration_ms.is_not(None),
                Image.pool == POOL_REAL,
            )
        ).all() if d
    )
    median_ms = durations[len(durations) // 2] if durations else None
    # 확정값(GT)은 만들지 않는다. 이 연구에서 의미 있는 진척도는
    # '몇 개의 (영상×항목)이 2인 이상에게서 계측되었나'다 — 비교 가능한 쌍의 수.
    paired = db.execute(
        select(Measurement.image_id, Measurement.variable_key)
        .join(Image, Image.id == Measurement.image_id)
        .where(Measurement.status == M_DONE, Image.pool == POOL_REAL)
        .group_by(Measurement.image_id, Measurement.variable_key)
        .having(func.count(func.distinct(Measurement.user_id)) >= 2)
    ).all()
    n_paired = len(paired)

    images = list(db.scalars(
        select(Image).where(Image.status == "active", Image.pool == POOL_REAL)
    ).all())
    counts = summarize(classify_all(db, images, tol))

    return _render(
        request, "admin_export.html", me, "export",
        n_images=int(n_images),
        n_measurements=int(n_measurements),
        n_redo=int(n_redo),
        median_ms=median_ms,
        n_paired=int(n_paired),
        n_reviewers=len(active_reviewers(db)),
        n_questions=int(n_images) * N_VARIABLES,
        counts=counts,
        tolerance_mm=tol,
    )


def _stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


@router.get("/export/wide.xlsx")
def export_wide(db: DbSession, me: AdminUser):
    tol = settings_store.tolerance_mm(db)
    df = export_mod.build_wide_frame(db, tol)
    data = export_mod.xlsx_bytes({"measurements": df})
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition":
                f'attachment; filename="picc_measurements_wide_{_stamp()}.xlsx"'
        },
    )


@router.get("/export/long.csv")
def export_long(db: DbSession, me: AdminUser):
    tol = settings_store.tolerance_mm(db)
    df = export_mod.build_long_frame(db, tol)
    return Response(
        content=export_mod.csv_bytes(df),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition":
                f'attachment; filename="picc_measurements_long_{_stamp()}.csv"'
        },
    )


@router.get("/export/disagreements.xlsx")
def export_disagreements(db: DbSession, me: AdminUser):
    tol = settings_store.tolerance_mm(db)
    df = export_mod.build_disagreement_frame(db, tol)
    data = export_mod.xlsx_bytes({"disagreements": df})
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition":
                f'attachment; filename="picc_disagreements_{_stamp()}.xlsx"'
        },
    )


@router.get("/export/bundle.zip")
def export_bundle(
    db: DbSession, me: AdminUser,
    media: Annotated[int, Query()] = 0,
):
    tol = settings_store.tolerance_mm(db)
    name = "picc_full_bundle" if media else "picc_data_bundle"
    return StreamingResponse(
        export_mod.stream_bundle(db, tol, include_media=bool(media)),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}_{_stamp()}.zip"'
        },
    )


@router.get("/export/dicom/{image_id}")
def export_single_dicom(db: DbSession, me: AdminUser, image_id: int):
    im = db.get(Image, image_id)
    if im is None:
        raise HTTPException(404, "영상을 찾을 수 없습니다.")
    path = DATA_DIR / im.dicom_path
    if not path.exists():
        raise HTTPException(404, "원본 파일이 없습니다.")
    return FileResponse(
        path, media_type="application/dicom", filename=im.original_filename
    )
