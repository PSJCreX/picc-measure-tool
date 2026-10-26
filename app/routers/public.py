"""로그인 · 로그아웃 · 비밀번호 변경 · 역할별 진입 분기."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.auth import hash_password, needs_rehash, verify_password
from app.deps import DbSession
from app.models import ROLE_ADMIN, User

router = APIRouter()
_templates: Jinja2Templates | None = None


def set_templates(t: Jinja2Templates) -> None:
    global _templates
    _templates = t


def tpl() -> Jinja2Templates:
    assert _templates is not None
    return _templates


def _home_for(user: User, db=None) -> str:
    """로그인 직후 갈 곳.

    계측을 다 끝낸 계측자는 **비교 화면**으로 보낸다 — 더 잴 것이 없는데 계측
    화면을 띄우면 "할 일이 없습니다"만 보게 된다. 단 그 화면은 서로의 계측값과
    AI 예측을 드러내므로, 관리자가 공개를 켜고 남은 작업이 0일 때만 열린다
    (`app/deps.py::matrix_view_open`).
    """
    if user.role == ROLE_ADMIN:
        return "/admin"
    if db is not None:
        from app.deps import matrix_view_open
        if matrix_view_open(db, user):
            return "/admin/matrix"
    return "/measure"


@router.get("/", response_class=HTMLResponse)
def root(request: Request, db: DbSession):
    uid = request.session.get("user_id")
    if not uid:
        return RedirectResponse("/login", status_code=302)
    user = db.get(User, int(uid))
    if not user or not user.is_active:
        request.session.clear()
        return RedirectResponse("/login", status_code=302)
    return RedirectResponse(_home_for(user, db), status_code=302)


@router.get("/login", response_class=HTMLResponse)
def login_get(request: Request, error: str | None = None):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=302)
    return tpl().TemplateResponse(
        "login.html", {"request": request, "error": error}
    )


@router.post("/login", response_class=HTMLResponse)
def login_post(
    request: Request,
    db: DbSession,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    user = db.scalar(select(User).where(User.username == username.strip()))
    # 존재하지 않는 계정과 틀린 비밀번호를 같은 메시지로 처리해 계정 존재 여부를 흘리지 않는다
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        return tpl().TemplateResponse(
            "login.html",
            {"request": request, "error": "아이디 또는 비밀번호가 올바르지 않습니다."},
            status_code=401,
        )

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        db.commit()

    request.session.clear()
    request.session["user_id"] = int(user.id)
    request.session["must_change_password"] = bool(user.must_change_password)
    if user.must_change_password:
        return RedirectResponse("/change-password", status_code=302)
    return RedirectResponse(_home_for(user, db), status_code=302)


@router.post("/logout")
@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


@router.get("/change-password", response_class=HTMLResponse)
def change_password_get(request: Request, db: DbSession):
    uid = request.session.get("user_id")
    if not uid:
        return RedirectResponse("/login", status_code=302)
    user = db.get(User, int(uid))
    if not user:
        request.session.clear()
        return RedirectResponse("/login", status_code=302)
    return tpl().TemplateResponse(
        "change_password.html",
        {"request": request, "me": user, "error": None,
         "forced": bool(user.must_change_password)},
    )


@router.post("/change-password", response_class=HTMLResponse)
def change_password_post(
    request: Request,
    db: DbSession,
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
):
    uid = request.session.get("user_id")
    if not uid:
        return RedirectResponse("/login", status_code=302)
    user = db.get(User, int(uid))
    if not user:
        request.session.clear()
        return RedirectResponse("/login", status_code=302)

    def fail(msg: str):
        return tpl().TemplateResponse(
            "change_password.html",
            {"request": request, "me": user, "error": msg,
             "forced": bool(user.must_change_password)},
            status_code=400,
        )

    if not verify_password(current_password, user.password_hash):
        return fail("현재 비밀번호가 올바르지 않습니다.")
    if len(new_password) < 8:
        return fail("새 비밀번호는 8자 이상이어야 합니다.")
    if new_password != confirm_password:
        return fail("새 비밀번호가 일치하지 않습니다.")
    if verify_password(new_password, user.password_hash):
        return fail("이전과 다른 비밀번호를 사용하세요.")

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    db.commit()

    request.session["must_change_password"] = False
    return RedirectResponse(_home_for(user, db), status_code=302)
