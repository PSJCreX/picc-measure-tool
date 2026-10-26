"""FastAPI 조립 — 미들웨어, 라우터, 정적 파일, 기동 시 초기화."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.bootstrap import ensure_admin_user
from app.config import (
    APP_TITLE,
    DATA_DIR,
    SECRET_KEY,
    STATIC_DIR,
    STUDY_TITLE,
    TEMPLATE_DIR,
)
from app.db import Base, SessionLocal, enable_sqlite_wal, engine, run_migrations
from app.routers import admin, public, reviewer


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    enable_sqlite_wal()
    run_migrations()
    db = SessionLocal()
    try:
        ensure_admin_user(db)
    finally:
        db.close()
    yield


class PasswordChangeGate(BaseHTTPMiddleware):
    """비밀번호 변경이 필요한 계정을 /change-password로 묶어둔다.

    관리자가 발급한 임시 비밀번호를 그대로 쓰는 계정이 계측을 시작하지 못하게 한다.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/static"):
            return await call_next(request)
        allow = {"/login", "/logout", "/change-password", "/favicon.ico", "/healthz"}
        if path in allow:
            return await call_next(request)
        if request.session.get("must_change_password") and request.session.get("user_id"):
            return RedirectResponse("/change-password", status_code=302)
        return await call_next(request)


app = FastAPI(title=APP_TITLE, lifespan=lifespan)

app.add_middleware(PasswordChangeGate)
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    # 90일 슬라이딩 세션 — 병원 PC는 계속 켜져 있고, 매번 로그인하게 하면
    # 전문의가 계측을 미룬다.
    max_age=90 * 24 * 3600,
)

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals["APP_TITLE"] = APP_TITLE
templates.env.globals["STUDY_TITLE"] = STUDY_TITLE

public.set_templates(templates)
reviewer.set_templates(templates)
admin.set_templates(templates)

# 유지보수(freeze) 플래그. 이 파일이 있으면 **쓰기를 전부 거부**한다.
#
# 왜 필요한가 — 서버 이전처럼 데이터를 복사하는 동안에는 새 계측이 들어오면
# 안 된다. 복사 시작 후 들어온 계측은 어느 쪽에도 온전히 남지 않는다.
# 그런데 예약 작업이 서버를 SYSTEM 계정으로 띄우므로 프로세스를 내릴 수 없는
# 경우가 있다. 프로세스를 죽이지 않고 '쓰기만' 멈추는 수단이 필요하다.
#
#   freeze : data/MAINTENANCE 파일을 만든다 (내용은 사용자에게 보일 사유)
#   해제   : 그 파일을 지운다
#
# 읽기(GET)는 그대로 통과시킨다 — 계측자가 자기 이력을 보는 것은 막을 이유가 없고,
# 무엇이 일어나는지 화면으로 알려 줄 수 있어야 한다.
MAINTENANCE_FLAG = DATA_DIR / "MAINTENANCE"


def maintenance_reason() -> str | None:
    """freeze 중이면 사유 문자열, 아니면 None."""
    try:
        if not MAINTENANCE_FLAG.exists():
            return None
        text = MAINTENANCE_FLAG.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or "점검 중입니다."


class MaintenanceGate(BaseHTTPMiddleware):
    """freeze 중에는 상태를 바꾸는 요청을 막는다."""

    SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

    async def dispatch(self, request: Request, call_next):
        reason = maintenance_reason()
        if reason is None or request.method in self.SAFE_METHODS:
            response = await call_next(request)
            if reason is not None:
                # 화면이 상태를 알 수 있게 헤더로도 알린다
                response.headers["X-Maintenance"] = "1"
            return response
        # 로그아웃은 막지 않는다 — 갇히는 느낌을 주지 않도록
        if request.url.path == "/logout":
            return await call_next(request)
        accept = request.headers.get("accept", "")
        msg = f"지금은 점검 중이라 저장할 수 없습니다. {reason}"
        if "text/html" in accept:
            return HTMLResponse(
                "<!doctype html><meta charset='utf-8'>"
                "<div style=\"font:16px/1.6 system-ui;max-width:34rem;"
                "margin:4rem auto;padding:0 1rem\">"
                f"<h2>점검 중</h2><p>{msg}</p>"
                "<p><a href='/measure'>계측 화면으로</a></p></div>",
                status_code=503,
            )
        return JSONResponse({"ok": False, "message": msg}, status_code=503)


class StaticNoCache(BaseHTTPMiddleware):
    """정적 파일은 **반드시 재검증**하게 만든다.

    왜 — Starlette의 StaticFiles는 ETag/Last-Modified만 주고 Cache-Control은 주지
    않는다. 그러면 브라우저가 휴리스틱 캐싱으로 재검증 없이 옛 파일을 계속 쓴다.
    실제로 이것 때문에 사고가 났다: upload.js 를 고쳐 업로드에 pool(연습/실제)을
    실어 보내게 했는데, 브라우저가 옛 upload.js 를 쓰는 바람에 pool 필드가 아예
    오지 않았고, 서버는 기본값 'real' 로 처리했다. **연습용으로 올린 영상이 전부
    실제 계측 pool 로 들어갔고, 화면 어디에도 이상이 보이지 않았다.**

    `no-cache` 는 '캐시하지 말라'가 아니라 '쓰기 전에 물어보라'다. 파일이 그대로면
    304만 오가므로 폐쇄망에서 비용이 사실상 없다. 조용히 틀리는 것보다 훨씬 낫다.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/static"):
            response.headers["Cache-Control"] = "no-cache"
            return response
        # HTML 화면도 재검증하게 만든다.
        #
        # 왜 — 화면이 가리키는 링크·버튼이 코드와 함께 바뀌는데, 브라우저가 옛
        # HTML 을 들고 있으면 **옛 화면을 조작하게 된다.** 실제로 그 때문에
        # 사고가 났다(옛 upload.js 를 쓰던 브라우저가 pool 값을 안 보내 연습용
        # 영상이 전부 실제 계측 pool 로 등재됐다). 페이지는 작고 폐쇄망이라
        # 매번 재검증해도 비용이 없다. 영상 전송(/image, /admin/image)은
        # 내용이 불변이라 제외한다 — 그쪽은 캐시가 이득이다.
        ctype = response.headers.get("content-type", "")
        if ctype.startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
        return response


app.add_middleware(StaticNoCache)
app.add_middleware(MaintenanceGate)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _asset_version(rel: str) -> str:
    """정적 파일의 수정시각 기반 버전. URL에 붙여 캐시를 확실히 갈아 끼운다.

    `no-cache` 만으로도 대부분 해결되지만, 중간에 캐싱 프록시가 있으면 그것마저
    무시될 수 있다. URL 자체가 달라지면 프록시도 새 파일로 본다.
    """
    try:
        return str(int((STATIC_DIR / rel).stat().st_mtime))
    except OSError:
        return "0"


def _matrix_open_for(request: Request) -> bool:
    """네비게이션이 '계측 비교' 링크를 보일지.

    화면마다 변수를 넘기면 빠뜨리는 곳이 생기므로 전역으로 둔다. 링크를 숨기는
    것은 안내일 뿐이고, 실제 차단은 라우트의 `MatrixViewer` 의존성이 한다.
    """
    uid = request.session.get("user_id") if hasattr(request, "session") else None
    if not uid:
        return False
    db = SessionLocal()
    try:
        from app.deps import matrix_view_open
        from app.models import User as _U
        u = db.get(_U, int(uid))
        return bool(u) and matrix_view_open(db, u)
    except Exception:
        return False
    finally:
        db.close()


templates.env.globals["matrix_open_for"] = _matrix_open_for
templates.env.globals["static_url"] = (
    lambda rel: f"/static/{rel}?v={_asset_version(rel)}"
)
# 렌더된 PNG는 data/ 아래에 있어 정적 마운트로 직접 노출하지 않는다.
# 계측 화면에서 /image/<id>/png 로 인증을 거쳐 받아간다 (reviewer 라우터).

app.include_router(public.router)
app.include_router(reviewer.router)
app.include_router(admin.router)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    svg = STATIC_DIR / "favicon.svg"
    if svg.is_file():
        return FileResponse(svg, media_type="image/svg+xml")
    return Response(status_code=204)


@app.get("/healthz", include_in_schema=False)
def healthz():
    """런처(start_server.ps1)와 Task Scheduler health-check가 찌르는 엔드포인트."""
    return {
        "ok": True,
        "data_dir": str(DATA_DIR),
        "maintenance": maintenance_reason(),
    }


@app.exception_handler(HTTPException)
async def auth_redirect(request: Request, exc: HTTPException):
    """브라우저 요청이면 로그인/홈으로 리다이렉트, API 요청이면 JSON 그대로."""
    accept = request.headers.get("accept", "")
    is_html = "text/html" in accept
    if exc.status_code == 401 and is_html:
        return RedirectResponse("/login", status_code=302)
    if exc.status_code == 403 and is_html:
        return RedirectResponse("/", status_code=302)
    return await http_exception_handler(request, exc)
