"""데이터 모델.

설계 메모
---------
* 계측의 단위는 **(영상 × 변수) 문제 하나**다. 영상 하나를 통째로 제출하는
  개념이 없으므로 `Measurement` 행 자체가 완료 표시다.
* 좌표는 **DICOM 원본 픽셀 좌표**로 저장한다. 기존 어노테이션
  (`data/annotations/<target>/*.json`)과 동일한 좌표계라 그대로 학습에 넣을 수 있고,
  렌더 해상도를 나중에 바꿔도 값이 깨지지 않는다.
* 길이 = 첫 점과 마지막 점 사이의 직선거리. 추론 코드와 동일하다
  (`calculate_clavicle_length`는 keypoints[0]↔[4],
   `calculate_two_vbu_length`는 유효 점의 첫↔끝을 쓴다).
* `duration_ms`는 **사진이 화면에 뜬 순간부터 마지막 drop까지**만 잰다.
  문제를 읽는 시간과 확정을 고민하는 시간은 빠진다 — 그래야 AI 추론 시간과
  같은 구간을 비교할 수 있다.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

ROLE_ADMIN = "admin"
ROLE_REVIEWER = "reviewer"

SPACING_FROM_PIXEL_SPACING = "PixelSpacing"
SPACING_FROM_IMAGER = "ImagerPixelSpacing"
SPACING_MANUAL = "manual"
SPACING_UNKNOWN = "unknown"

# 영상 pool — 연습용과 실제 계측용을 완전히 분리한다.
# 연습 기록은 어떤 내보내기·집계에도 들어가지 않는다.
POOL_REAL = "real"          # 논문에 쓰이는 실제 계측 대상
POOL_PRACTICE = "practice"  # 연습 전용. 마음껏 재고 지워도 된다.
POOL_LABELS = {POOL_REAL: "실제 계측", POOL_PRACTICE: "연습"}

# 계측 상태
M_DONE = "done"   # 유효한 현재 계측
M_REDO = "redo"   # 계측자가 재측정을 요청함 — 집계 제외, 대기 pool로 회귀


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default=ROLE_REVIEWER, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def display_label(self) -> str:
        n = (self.full_name or "").strip()
        return n if n else self.username


class Image(Base):
    """업로드된 DICOM 1장 + 렌더 산출물. 모든 영상이 8개 변수의 대상이다."""

    __tablename__ = "images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 같은 파일을 두 번 올려도 중복 등재되지 않도록
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    # data/ 기준 상대경로 — 데이터 폴더를 통째로 옮겨도 살아남는다
    dicom_path: Mapped[str] = mapped_column(String(400))
    png_path: Mapped[str] = mapped_column(String(400))
    png_enh_path: Mapped[str | None] = mapped_column(String(400), nullable=True)

    orig_w: Mapped[int] = mapped_column(Integer)
    orig_h: Mapped[int] = mapped_column(Integer)
    # png 픽셀 → 원본 픽셀 환산용. orig_w * render_scale == png 폭
    render_scale: Mapped[float] = mapped_column(Float, default=1.0)

    # mm/pixel (원본 해상도 기준). 해석 불가하면 None.
    pixel_spacing_mm: Mapped[float | None] = mapped_column(Float, nullable=True)
    spacing_source: Mapped[str] = mapped_column(String(32), default=SPACING_UNKNOWN)

    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    # 연습용인지 실제 계측용인지. 기존 영상은 전부 real 로 남는다.
    pool: Mapped[str] = mapped_column(String(16), default=POOL_REAL, index=True)
    notes: Mapped[str] = mapped_column(Text, default="")

    uploaded_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    measurements: Mapped[list["Measurement"]] = relationship(
        back_populates="image", cascade="all, delete-orphan"
    )

    @property
    def has_spacing(self) -> bool:
        return self.pixel_spacing_mm is not None and self.pixel_spacing_mm > 0


class Prediction(Base):
    """AI 모델이 낸 예측 — (영상 × 항목) 하나.

    **`Measurement` 와 섞지 않는 이유.** 불일치 판정(`spread_mm`)과 내보내기는
    '사람끼리의 차이'를 재도록 만들어져 있다. AI 를 계측자 한 명으로 끼워 넣으면
    interobserver agreement 수치가 오염된다 — 그건 논문 자료를 망친다.
    AI 는 비교 대상이지 또 한 명의 관찰자가 아니다.

    시간을 두 가지로 나눠 저장한다
    ------------------------------
    * `infer_ms`      — 네트워크 순수 추론 (워밍업 후 중앙값)
    * `preprocess_ms` — DICOM 로드 + 512 리사이즈·패딩. 영상당 한 번이고
                        8항목이 공유하므로 항목마다 같은 값이 들어간다.

    사람 쪽 `duration_ms` 와 무엇을 맞댈지는 논문에서 정할 문제다. 둘 다 재 두면
    나중에 다시 돌리지 않아도 된다.
    """

    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    image_id: Mapped[int] = mapped_column(
        ForeignKey("images.id", ondelete="CASCADE"), index=True
    )
    variable_key: Mapped[str] = mapped_column(String(40), index=True)

    points: Mapped[list] = mapped_column(JSON)       # DICOM 원본 px — 계측과 같은 좌표계
    value_mm: Mapped[float | None] = mapped_column(Float, nullable=True)

    infer_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    preprocess_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 어떤 조건에서 낸 값인지 — 재현에 필요하다
    device: Mapped[str] = mapped_column(String(32), default="")      # cuda / cpu
    device_name: Mapped[str] = mapped_column(String(80), default="") # NVIDIA GB10
    torch_version: Mapped[str] = mapped_column(String(32), default="")
    model_file: Mapped[str] = mapped_column(String(120), default="")
    notes: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # 같은 (영상, 항목)은 한 벌만 — 다시 돌리면 덮어쓴다
    __table_args__ = (
        UniqueConstraint("image_id", "variable_key", name="uq_prediction_image_var"),
    )

    image: Mapped[Image] = relationship()


class Measurement(Base):
    """한 문제의 답 — 한 리뷰어가 한 영상의 한 변수를 잰 결과.

    재측정을 요청하면 그 행은 `status='redo'`가 되고 집계에서 빠진다.
    같은 (영상, 변수)가 대기 pool로 돌아가 나중에 무작위로 다시 출제되며,
    그때 `attempt`가 하나 올라간 새 행이 쌓인다. 버려진 시도도 지우지 않는다 —
    몇 번 만에 만족했는지가 그 자체로 자료다.
    """

    __tablename__ = "measurements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    image_id: Mapped[int] = mapped_column(
        ForeignKey("images.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    variable_key: Mapped[str] = mapped_column(String(40), index=True)

    # [[x, y], [x, y]] — DICOM 원본 픽셀 좌표. 드래그 시작점과 끝점.
    points: Mapped[list] = mapped_column(JSON, default=list)
    value_mm: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 사진이 뜬 순간 → 마지막 drop. 확정을 고민한 시간은 포함하지 않는다.
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default=M_DONE, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    image: Mapped[Image] = relationship(back_populates="measurements")
    user: Mapped[User] = relationship()

    __table_args__ = (
        UniqueConstraint(
            "image_id", "user_id", "variable_key", "attempt",
            name="uq_measure_img_user_var_attempt",
        ),
    )


class Setting(Base):
    """관리자가 바꾸는 런타임 설정. 기본값은 config.DEFAULT_SETTINGS."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
