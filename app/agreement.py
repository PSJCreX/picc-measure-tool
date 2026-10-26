"""관찰자간 일치도(interobserver agreement) 판정.

**확정값(GT)을 만드는 곳이 아니다.** 이 연구는 전문의들의 계측을 하나의 값으로
합의시키지 않는다. 2인 이상이 같은 (영상, 항목)을 각자 독립적으로 재고,
그 계측들이 **그대로** 분석 대상이 된다.

여기서 하는 일은 하나뿐이다 — **사람끼리 얼마나 다르게 재는지를 기록한다.**
AI 오차가 크다 작다를 말하려면 전문의끼리의 변동폭부터 알아야 하기 때문이다.
전문의 둘이 3mm 다르게 재는 항목에서 AI가 3mm 벗어난 것은 다른 의미다.

판정 규칙 — (영상, 항목) 단위로 현재 유효한(`status='done'`) 계측을 모은 뒤

    n < 2                                → partial   (비교 불가)
    max(values) - min(values) > tol_mm   → mismatch
    그 외                                 → agree

`tol_mm`은 관리자가 정하는 전역 허용범위다. 이 판정은 **보고용 지표일 뿐**
계측값을 바꾸거나 버리거나 대체하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    M_DONE,
    ROLE_REVIEWER,
    Image,
    Measurement,
    User,
)
from app.variables import ALL_VARIABLE_KEYS

KIND_AGREE = "agree"
KIND_MISMATCH = "mismatch"
KIND_PARTIAL = "partial"
KIND_EMPTY = "empty"

KIND_LABELS = {
    KIND_AGREE: "일치",
    KIND_MISMATCH: "불일치",
    KIND_PARTIAL: "일부만 계측",
    KIND_EMPTY: "계측 없음",
}


# ----------------------------------------------------------------------------
# 조회 헬퍼
# ----------------------------------------------------------------------------


def active_reviewers(db: Session) -> list[User]:
    """일치도 집계의 분모가 되는 활성 계측자. 관리자는 제외한다."""
    return list(
        db.scalars(
            select(User)
            .where(User.role == ROLE_REVIEWER, User.is_active.is_(True))
            .order_by(User.id)
        ).all()
    )


def current_measurements(
    db: Session, image_ids: list[int] | None = None
) -> dict[int, dict[int, dict[str, Measurement]]]:
    """{image_id: {user_id: {variable_key: Measurement}}} — 유효한 계측만."""
    stmt = select(Measurement).where(Measurement.status == M_DONE)
    if image_ids is not None:
        if not image_ids:
            return {}
        stmt = stmt.where(Measurement.image_id.in_(image_ids))
    out: dict[int, dict[int, dict[str, Measurement]]] = {}
    for m in db.scalars(stmt).all():
        out.setdefault(m.image_id, {}).setdefault(m.user_id, {})[m.variable_key] = m
    return out


# ----------------------------------------------------------------------------
# 판정
# ----------------------------------------------------------------------------


@dataclass
class VariableAgreement:
    variable_key: str
    kind: str
    values: dict[int, float]           # user_id -> mm
    spread_mm: float | None = None     # max - min
    mean_mm: float | None = None

    @property
    def n(self) -> int:
        return len(self.values)


@dataclass
class ImageAgreement:
    image_id: int
    kind: str
    per_variable: dict[str, VariableAgreement] = field(default_factory=dict)
    n_measured: int = 0                # 이 영상에 쌓인 계측 수 (계측자 × 항목)
    n_expected: int = 0                # 활성 계측자 수 × 8

    @property
    def mismatched_keys(self) -> list[str]:
        return [k for k, v in self.per_variable.items() if v.kind == KIND_MISMATCH]


def classify_variable(
    variable_key: str,
    values_by_user: dict[int, float],
    tolerance_mm: float,
) -> VariableAgreement:
    values = list(values_by_user.values())
    if len(values) == 0:
        return VariableAgreement(variable_key, KIND_EMPTY, values_by_user)
    if len(values) < 2:
        return VariableAgreement(
            variable_key, KIND_PARTIAL, values_by_user,
            spread_mm=0.0, mean_mm=values[0],
        )
    spread = max(values) - min(values)
    mean = sum(values) / len(values)
    kind = KIND_MISMATCH if spread > tolerance_mm else KIND_AGREE
    return VariableAgreement(variable_key, kind, values_by_user, spread, mean)


def classify_image(
    image: Image,
    reviewers: list[User],
    per_user_measurements: dict[int, dict[str, Measurement]],
    tolerance_mm: float,
) -> ImageAgreement:
    n_reviewers = len(reviewers)
    result = ImageAgreement(
        image_id=image.id,
        kind=KIND_EMPTY,
        n_expected=n_reviewers * len(ALL_VARIABLE_KEYS),
    )

    n_measured = 0
    for key in ALL_VARIABLE_KEYS:
        values_by_user: dict[int, float] = {}
        for reviewer in reviewers:
            m = per_user_measurements.get(reviewer.id, {}).get(key)
            if m is not None and m.value_mm is not None:
                values_by_user[reviewer.id] = float(m.value_mm)
        n_measured += len(values_by_user)
        result.per_variable[key] = classify_variable(key, values_by_user, tolerance_mm)

    result.n_measured = n_measured
    kinds = [v.kind for v in result.per_variable.values()]
    if KIND_MISMATCH in kinds:
        result.kind = KIND_MISMATCH
    elif all(k == KIND_EMPTY for k in kinds):
        result.kind = KIND_EMPTY
    elif KIND_PARTIAL in kinds or KIND_EMPTY in kinds:
        result.kind = KIND_PARTIAL
    else:
        result.kind = KIND_AGREE
    return result


def classify_all(
    db: Session,
    images: list[Image],
    tolerance_mm: float,
) -> dict[int, ImageAgreement]:
    """여러 영상을 한 번에 판정. 매트릭스/대시보드/내보내기 공용 진입점."""
    reviewers = active_reviewers(db)
    image_ids = [im.id for im in images]
    per_image = current_measurements(db, image_ids)
    return {
        im.id: classify_image(
            im, reviewers, per_image.get(im.id, {}), tolerance_mm
        )
        for im in images
    }


# ----------------------------------------------------------------------------
# 요약 통계 (대시보드용)
# ----------------------------------------------------------------------------


def summarize(agreements: dict[int, ImageAgreement]) -> dict[str, int]:
    counts = {KIND_AGREE: 0, KIND_MISMATCH: 0, KIND_PARTIAL: 0, KIND_EMPTY: 0}
    n_var_mismatch = 0
    for agr in agreements.values():
        counts[agr.kind] = counts.get(agr.kind, 0) + 1
        n_var_mismatch += len(agr.mismatched_keys)
    counts["variable_mismatches"] = n_var_mismatch
    return counts
