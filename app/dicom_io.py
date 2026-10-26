"""DICOM 읽기 · 픽셀 스페이싱 해석 · PNG 렌더 · 이미지 강화.

픽셀 스페이싱 (이 모듈의 존재 이유)
------------------------------------
Test set 519장을 전수 조사한 결과, `PixelSpacing`(0028,0030)이 없는 파일이
Lt 24.9%(97/390), Rt 20.2%(26/129)였고 `ImagerPixelSpacing`(0018,1164)은
**100% 존재**했다. 실제 값도 0.14 / 0.1988 / 0.194222 / 0.187594 … 로 균일하지 않다.

기존 추론 코드는 `PixelSpacing`만 읽고 없으면 0.2mm(CLI) 또는 0.831mm(GUI) 고정값으로
떨어진다 — test set의 약 23%가 잘못된 mm 환산을 받는다.
여기서는 `PixelSpacing` → `ImagerPixelSpacing` → 수동입력 순으로 해석해 이를 피한다.

전송 구문
---------
Test set의 94%가 JPEG2000(1.2.840.10008.1.2.4.91)이다. `pylibjpeg-openjpeg`가
주 디코더이고 `python-gdcm`이 폴백이다. 둘 다 requirements.txt에 있다.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pydicom

# pydicom 3.x는 VOI/Modality LUT 헬퍼를 pydicom.pixels로 옮겼다.
# 2.x 경로는 3.x에서 deprecated shim으로만 남아 있고 4.0에서 사라진다.
try:
    from pydicom.pixels import apply_modality_lut, apply_voi_lut
except ImportError:  # pydicom 2.x
    from pydicom.pixel_data_handlers.util import apply_modality_lut, apply_voi_lut

from app.models import (
    SPACING_FROM_IMAGER,
    SPACING_FROM_PIXEL_SPACING,
    SPACING_UNKNOWN,
)

# 데스크톱 라벨링 툴(picc_labeling_tool.py:358)과 같은 CLAHE 파라미터.
# CLAHE는 타일 개수 기준(8x8)이라 해상도가 달라도 효과가 같다.
CLAHE_CLIP_LIMIT = 2.0
CLAHE_TILE_GRID = (8, 8)

# 샤프닝은 사정이 다르다. 데스크톱 툴은 긴 변 800px 이미지에 3x3 커널
# [[-1,-1,-1],[-1,9,-1],[-1,-1,-1]] 을 쓴다. 같은 커널을 2048px에 그대로 적용하면
# 훨씬 더 미세한 성분을 증폭해 노이즈만 튀고 뼈 경계는 덜 살아난다.
# 그래서 커널 대신 **해상도에 비례하는 언샤프 마스크**로 바꿔 800px에서의
# 시각적 효과를 그대로 재현한다.
SHARPEN_REFERENCE_PX = 800     # 데스크톱 툴의 표시 해상도
SHARPEN_BASE_SIGMA = 0.8       # 800px에서 3x3 커널에 대응하는 가우시안 반경
SHARPEN_AMOUNT = 1.1           # 언샤프 강도


class DicomError(RuntimeError):
    """DICOM을 읽거나 렌더할 수 없을 때."""


@dataclass
class DicomMeta:
    orig_w: int
    orig_h: int
    pixel_spacing_mm: float | None
    spacing_source: str
    modality: str
    photometric: str


# ----------------------------------------------------------------------------
# 스페이싱 해석
# ----------------------------------------------------------------------------


def _first_spacing_value(raw) -> float | None:
    """`['0.14','0.14']` 또는 `'0.14\\0.14'` 형태에서 행 간격(첫 값)을 뽑는다."""
    if raw is None:
        return None
    try:
        if isinstance(raw, (list, tuple)) or hasattr(raw, "__len__") and not isinstance(raw, str):
            if len(raw) == 0:
                return None
            value = float(raw[0])
        else:
            text = str(raw)
            value = float(text.split("\\")[0])
    except (TypeError, ValueError, IndexError):
        return None
    return value if value > 0 else None


def resolve_pixel_spacing(ds: pydicom.Dataset) -> tuple[float | None, str]:
    """(mm/pixel, 출처). PixelSpacing → ImagerPixelSpacing → unknown 순.

    ImagerPixelSpacing은 검출기 표면 기준이라 확대율 보정 전 값이지만,
    이 데이터셋의 촬영 프로토콜이 동일하고 기존 학습/추론도 같은 전제를 쓰므로
    일관성 측면에서 그대로 사용한다.
    """
    v = _first_spacing_value(getattr(ds, "PixelSpacing", None))
    if v is not None:
        return v, SPACING_FROM_PIXEL_SPACING
    v = _first_spacing_value(getattr(ds, "ImagerPixelSpacing", None))
    if v is not None:
        return v, SPACING_FROM_IMAGER
    return None, SPACING_UNKNOWN


# ----------------------------------------------------------------------------
# 픽셀 → 8bit 그레이스케일
# ----------------------------------------------------------------------------


def _to_uint8(ds: pydicom.Dataset) -> np.ndarray:
    """표시용 8bit 배열. VOI LUT → MONOCHROME1 반전 → min-max 정규화."""
    try:
        arr = ds.pixel_array
    except Exception as exc:  # 디코더 부재/손상
        raise DicomError(f"픽셀 디코딩 실패: {exc}") from exc

    if arr.ndim == 3:
        # 컬러(드묾) → 그레이스케일
        arr = arr[..., :3].mean(axis=2)

    arr = arr.astype(np.float32)

    # RescaleSlope/Intercept
    try:
        arr = apply_modality_lut(arr, ds).astype(np.float32)
    except Exception:
        pass

    # Window Center/Width. 태그가 없으면 원본을 그대로 쓴다(=min-max로 넘어감).
    if "WindowCenter" in ds or "VOILUTSequence" in ds:
        try:
            arr = apply_voi_lut(arr, ds).astype(np.float32)
        except Exception:
            pass

    # MONOCHROME1은 값이 클수록 어두운 규약 — 반전해야 일반적인 X-ray로 보인다.
    if str(getattr(ds, "PhotometricInterpretation", "")).strip() == "MONOCHROME1":
        arr = arr.max() - arr

    lo, hi = float(arr.min()), float(arr.max())
    if hi - lo < 1e-6:
        return np.zeros(arr.shape, dtype=np.uint8)
    arr = (arr - lo) / (hi - lo) * 255.0
    return arr.astype(np.uint8)


# ----------------------------------------------------------------------------
# 공개 API
# ----------------------------------------------------------------------------


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_meta(path: Path) -> DicomMeta:
    """픽셀을 디코딩하지 않고 헤더만 읽는다(업로드 검증용, 빠름)."""
    try:
        ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
    except Exception as exc:
        raise DicomError(f"DICOM 헤더를 읽을 수 없습니다: {exc}") from exc

    rows = int(getattr(ds, "Rows", 0) or 0)
    cols = int(getattr(ds, "Columns", 0) or 0)
    if rows <= 0 or cols <= 0:
        raise DicomError("Rows/Columns 태그가 없습니다. 영상 DICOM이 아닙니다.")

    spacing, source = resolve_pixel_spacing(ds)
    return DicomMeta(
        orig_w=cols,
        orig_h=rows,
        pixel_spacing_mm=spacing,
        spacing_source=source,
        modality=str(getattr(ds, "Modality", "") or ""),
        photometric=str(getattr(ds, "PhotometricInterpretation", "") or ""),
    )


def render_png(src: Path, dst: Path, max_px: int) -> tuple[int, int, float]:
    """DICOM → 8bit 그레이스케일 PNG. (png_w, png_h, render_scale) 반환.

    render_scale = png_w / orig_w. 좌표 환산에 쓴다.
    긴 변이 max_px 이하면 원본 해상도를 그대로 유지한다(축소만 하고 확대는 안 함).
    """
    try:
        ds = pydicom.dcmread(str(src), force=True)
    except Exception as exc:
        raise DicomError(f"DICOM을 읽을 수 없습니다: {exc}") from exc

    img = _to_uint8(ds)
    h, w = img.shape[:2]

    longest = max(w, h)
    if longest > max_px:
        scale = max_px / float(longest)
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        # 축소는 INTER_AREA가 모아레 없이 가장 깨끗하다
        img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)
    else:
        new_w, new_h = w, h

    dst.parent.mkdir(parents=True, exist_ok=True)
    ok = cv2.imwrite(str(dst), img, [cv2.IMWRITE_PNG_COMPRESSION, 4])
    if not ok:
        raise DicomError(f"PNG 저장 실패: {dst}")

    return new_w, new_h, new_w / float(w)


def enhance_png(src: Path, dst: Path) -> None:
    """CLAHE + 샤프닝 강화본 생성.

    데스크톱 라벨링 툴의 `apply_image_enhancement`(picc_labeling_tool.py:358)와
    동일한 파라미터를 쓴다 — 전문의가 두 도구에서 같은 화면을 보게 하려는 것.
    """
    img = cv2.imread(str(src), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise DicomError(f"PNG를 읽을 수 없습니다: {src}")

    clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP_LIMIT, tileGridSize=CLAHE_TILE_GRID)
    out = clahe.apply(img)

    # 해상도 비례 언샤프 마스크
    longest = max(img.shape[:2])
    sigma = SHARPEN_BASE_SIGMA * max(1.0, longest / SHARPEN_REFERENCE_PX)
    blurred = cv2.GaussianBlur(out, (0, 0), sigma)
    out = cv2.addWeighted(
        out, 1.0 + SHARPEN_AMOUNT, blurred, -SHARPEN_AMOUNT, 0
    )
    out = np.clip(out, 0, 255).astype(np.uint8)

    dst.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(dst), out, [cv2.IMWRITE_PNG_COMPRESSION, 4]):
        raise DicomError(f"강화 PNG 저장 실패: {dst}")


def decoder_status() -> dict[str, bool]:
    """설치된 픽셀 디코더 확인 — /admin 현황에 표시해 배포 사고를 막는다."""
    status: dict[str, bool] = {}
    for label, module in (
        ("pylibjpeg-openjpeg (JPEG2000)", "openjpeg"),
        ("pylibjpeg-libjpeg (JPEG)", "libjpeg"),
        ("python-gdcm (폴백)", "gdcm"),
    ):
        try:
            __import__(module)
            status[label] = True
        except Exception:
            status[label] = False
    return status
