"""설정 — 환경변수(.env) + 코드 기본값.

CWD와 무관하게 web_labeling/.env를 읽는다. 값이 비어 있으면(`KEY=` 만 적은 경우 포함)
코드 기본값으로 떨어진다.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(key: str, default: str) -> str:
    raw = os.getenv(key)
    if raw is None:
        return default
    stripped = raw.strip()
    return default if not stripped else stripped


# ----------------------------------------------------------------------------
# 경로
# ----------------------------------------------------------------------------
# 데이터 폴더는 통째로 옮길 수 있게 환경변수로 뺀다. 원본 DICOM이 쌓이면
# 수 GB가 되므로 다른 드라이브를 가리키게 할 수 있어야 한다.
DATA_DIR = Path(_env("DATA_DIR", str(BASE_DIR / "data"))).resolve()
DICOM_DIR = DATA_DIR / "dicom"
PNG_DIR = DATA_DIR / "png"
LOG_DIR = DATA_DIR / "logs"

TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

for _d in (DATA_DIR, DICOM_DIR, PNG_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DATABASE_URL = _env("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'app.db').as_posix()}")


# ----------------------------------------------------------------------------
# 보안
# ----------------------------------------------------------------------------
def _resolve_secret_key() -> str:
    """세션 서명 키. .env에 없으면 생성해 data/secret_key.txt에 보관한다.

    파일로 남겨야 서버를 재시작해도 로그인 세션이 유지된다(재부팅 자동 기동 요구사항).
    """
    from_env = os.getenv("SECRET_KEY", "").strip()
    if from_env:
        return from_env
    key_file = DATA_DIR / "secret_key.txt"
    if key_file.exists():
        existing = key_file.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    generated = secrets.token_urlsafe(48)
    key_file.write_text(generated, encoding="utf-8")
    try:  # 소유자만 읽도록 (Windows에서는 무시됨)
        key_file.chmod(0o600)
    except OSError:
        pass
    return generated


SECRET_KEY = _resolve_secret_key()

INITIAL_ADMIN_USERNAME = _env("INITIAL_ADMIN_USERNAME", "admin")
# 기본값을 두지 않는다 — 공개 배포본에서 admin/admin 으로 조용히 계정이
# 만들어지면 그 자체가 결함이다. 비어 있으면 bootstrap 이 기동을 거부한다.
INITIAL_ADMIN_PASSWORD = _env("INITIAL_ADMIN_PASSWORD", "")


# ----------------------------------------------------------------------------
# 서버
# ----------------------------------------------------------------------------
# 0.0.0.0 이면 동일 LAN의 다른 기기(전문의 PC)에서 접속 가능.
# 포트 8100 — 같은 연구실의 ABDXAI(8000)/Report AI(8000)와 충돌하지 않도록.
# ----------------------------------------------------------------------------
# AI 추론 환경 (선택)
# ----------------------------------------------------------------------------
# 추론은 웹과 **다른 venv** 에서 돌린다 — 웹툴은 numpy 2.x + pydicom 3.x,
# 추론은 torch(+CUDA 3GB)를 쓰기 때문에 한 venv 에 몰면 서로를 끌어내린다.
# 비워 두면 AI 계측 기능만 비활성이고 나머지는 전부 동작한다.
INFER_PYTHON = Path(_env("INFER_PYTHON", str(BASE_DIR / ".infer" / "bin" / "python")))
INFER_SCRIPT = Path(_env("INFER_SCRIPT", str(BASE_DIR / "scripts" / "predict_stream.py")))

WEBAPP_HOST = _env("WEBAPP_HOST", "0.0.0.0")
WEBAPP_PORT = int(_env("WEBAPP_PORT", "8100"))

APP_TITLE = _env("APP_TITLE", "PICC 계측 라벨링")
STUDY_TITLE = _env(
    "STUDY_TITLE",
    "흉부 단순촬영 기반 PICC 삽입 길이 예측 모델의 외부 검증 — 전문의 계측 연구",
)


# ----------------------------------------------------------------------------
# 기본 설정값 (관리자가 /admin/settings에서 변경 → Setting 테이블에 저장)
# ----------------------------------------------------------------------------
DEFAULT_SETTINGS: dict[str, str] = {
    # 관찰자간 불일치 판정 기준. 같은 (영상, 변수)에 대한 계측값의
    # (최댓값 − 최솟값)이 이 값을 넘으면 불일치로 본다. 전역 단일값.
    "tolerance_mm": "5.0",
    # DICOM → PNG 렌더 시 긴 변 최대 픽셀. 데스크톱 툴은 800이지만
    # 계측 정밀도가 이 도구의 존재 이유라 훨씬 크게 잡는다.
    "render_max_px": "2048",
    # 무조작 잠금까지 시간(ms). Report AI 웹앱과 동일한 60초.
    "idle_lock_ms": "60000",
    # idle phase1 → phase2 전환까지 시간(ms).
    "idle_phase1_ms": "10000",
    # 계측 1건당 소요시간 상한(ms). 이상값 완화용 24시간.
    "max_duration_ms": "86400000",
    # 계측자에게 비교 화면(매트릭스·영상 상세)을 공개할지. **기본 꺼짐.**
    #
    # 켜면 계측자가 서로의 계측값과 AI 예측을 본다 — 눈가림이 깨진다. 계측이
    # 전부 끝난 뒤에만 켜야 한다. "남은 작업 0" 을 자동 조건으로 쓰지 않는 이유:
    # 아직 업로드하지 않은 영상이 있으면 지금 0 이어도 연구가 끝난 게 아니다
    # (test set 519장 중 159장만 올라간 상태였다). 켜는 것은 관리자의 판단이어야
    # 한다. 켜도 남은 작업이 있는 계측자에게는 열리지 않는다.
    "reviewer_review_access": "0",
}
