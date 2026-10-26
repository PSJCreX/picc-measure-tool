"""좌표 → 길이 환산.

계측값 정의는 추론 코드와 정확히 같다:
  * `calculate_clavicle_length`  → keypoints[0] ↔ keypoints[4]  (5점 중 첫↔끝)
  * `calculate_two_vbu_length`   → 유효 점의 첫 ↔ 끝
  * HVD / TCD / 2nd ribs         → keypoints[0] ↔ keypoints[1]

즉 어떤 변수든 **첫 점과 마지막 점 사이의 직선거리**다. 중간 점은 형태 기록용이며
길이에 영향을 주지 않는다. 그래서 저장 구조를 '순서 있는 점 목록' 하나로 통일했다.
"""
from __future__ import annotations

import math


def normalize_points(raw) -> list[list[float]]:
    """클라이언트가 보낸 좌표를 [[x, y], ...] float 리스트로 정리."""
    out: list[list[float]] = []
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            x = float(item[0])
            y = float(item[1])
        except (TypeError, ValueError):
            continue
        if math.isnan(x) or math.isnan(y) or math.isinf(x) or math.isinf(y):
            continue
        out.append([x, y])
    return out


def pixel_length(points: list[list[float]]) -> float | None:
    """첫 점 ↔ 마지막 점의 픽셀 거리. 점이 2개 미만이면 None."""
    pts = normalize_points(points)
    if len(pts) < 2:
        return None
    (x1, y1), (x2, y2) = pts[0], pts[-1]
    return math.hypot(x2 - x1, y2 - y1)


def value_mm(points: list[list[float]], pixel_spacing_mm: float | None) -> float | None:
    """DICOM 원본 픽셀 좌표 + mm/pixel → 길이(mm).

    스페이싱을 모르면 None. 값 없이 저장은 되지만 일치도 판정에서는 빠진다.
    """
    px = pixel_length(points)
    if px is None or not pixel_spacing_mm or pixel_spacing_mm <= 0:
        return None
    return px * float(pixel_spacing_mm)


def mm_to_cm(mm: float | None) -> float | None:
    """논문 공식은 cm를 쓴다. 표시·검산용."""
    return None if mm is None else mm / 10.0
