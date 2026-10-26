"""업로드된 DICOM 등재 — 중복 검사 · 저장 · PNG 렌더.

파일 하나당 결과를 dict로 돌려주고, 라우터가 그걸 그대로 JSON으로 흘려보낸다.
업로드 진행률 바가 파일별 상태(등재/중복/오류)를 표시하는 근거가 된다.
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import DICOM_DIR, PNG_DIR
from app.dicom_io import DicomError, read_meta, render_png, sha256_of
from app.models import POOL_REAL, Image

# 확장자가 없는 DICOM도 흔해서 이름만으로 거르지 않는다. ZIP 안에서만
# 명백한 비-DICOM(썸네일, 메모)을 제외하는 용도로 쓴다.
_SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff",
    ".txt", ".md", ".csv", ".xlsx", ".xls", ".pdf", ".json",
    ".db", ".ini", ".ds_store",
}


@dataclass
class IngestResult:
    filename: str
    status: str          # ok | duplicate | error
    image_id: int | None = None
    message: str = ""
    pixel_spacing_mm: float | None = None
    spacing_source: str = ""


def _shard_paths(sha: str) -> tuple[Path, Path]:
    """sha 앞 2글자로 폴더를 나눈다 — 한 폴더에 수천 파일이 쌓이면 탐색기가 느려진다."""
    shard = sha[:2]
    return (
        DICOM_DIR / shard / f"{sha}.dcm",
        PNG_DIR / shard / f"{sha}.png",
    )


def _rel(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def ingest_one(
    db: Session,
    *,
    filename: str,
    content: bytes,
    uploaded_by: int | None,
    render_max_px: int,
    pool: str = POOL_REAL,
) -> IngestResult:
    from app.config import DATA_DIR

    if not content:
        return IngestResult(filename, "error", message="빈 파일입니다.")

    sha = sha256_of(content)
    existing = db.scalar(select(Image).where(Image.sha256 == sha))
    if existing is not None:
        return IngestResult(
            filename, "duplicate", image_id=int(existing.id),
            message=f"이미 등재된 영상입니다 (#{existing.id}).",
        )

    dicom_path, png_path = _shard_paths(sha)
    dicom_path.parent.mkdir(parents=True, exist_ok=True)
    dicom_path.write_bytes(content)

    try:
        meta = read_meta(dicom_path)
    except DicomError as exc:
        dicom_path.unlink(missing_ok=True)
        return IngestResult(filename, "error", message=str(exc))

    try:
        png_w, png_h, scale = render_png(dicom_path, png_path, render_max_px)
    except DicomError as exc:
        dicom_path.unlink(missing_ok=True)
        png_path.unlink(missing_ok=True)
        # JPEG2000 디코더가 없으면 여기서 대부분 걸린다 — 원인을 알려준다
        return IngestResult(
            filename, "error",
            message=f"{exc} (JPEG2000 영상이면 pylibjpeg-openjpeg 설치를 확인하세요)",
        )

    image = Image(
        sha256=sha,
        original_filename=filename[:255],
        dicom_path=_rel(dicom_path, DATA_DIR),
        png_path=_rel(png_path, DATA_DIR),
        png_enh_path=None,
        orig_w=meta.orig_w,
        orig_h=meta.orig_h,
        render_scale=scale,
        pixel_spacing_mm=meta.pixel_spacing_mm,
        spacing_source=meta.spacing_source,
        status="active",
        pool=pool,
        uploaded_by=uploaded_by,
    )
    db.add(image)
    db.commit()
    db.refresh(image)

    note = ""
    if meta.pixel_spacing_mm is None:
        note = "픽셀 스페이싱을 찾을 수 없습니다. 설정에서 수동 입력해야 mm 계측이 가능합니다."

    return IngestResult(
        filename, "ok", image_id=int(image.id), message=note,
        pixel_spacing_mm=meta.pixel_spacing_mm,
        spacing_source=meta.spacing_source,
    )


def iter_zip_members(content: bytes) -> list[tuple[str, bytes]]:
    """ZIP 안의 DICOM 후보를 (이름, 바이트)로 뽑는다."""
    out: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            base = Path(name).name
            if not base or base.startswith("."):
                continue
            if Path(base).suffix.lower() in _SKIP_SUFFIXES:
                continue
            out.append((base, zf.read(info)))
    return out


def ingest_upload(
    db: Session,
    *,
    filename: str,
    content: bytes,
    uploaded_by: int | None,
    render_max_px: int,
    pool: str = POOL_REAL,
) -> list[dict]:
    """단일 파일 또는 ZIP을 등재하고 결과 목록을 반환."""
    results: list[IngestResult] = []

    if filename.lower().endswith(".zip"):
        try:
            members = iter_zip_members(content)
        except zipfile.BadZipFile:
            return [asdict(IngestResult(filename, "error", message="손상된 ZIP입니다."))]
        if not members:
            return [asdict(IngestResult(filename, "error", message="ZIP에 DICOM이 없습니다."))]
        for name, data in members:
            results.append(
                ingest_one(
                    db, filename=name, content=data,
                    uploaded_by=uploaded_by, render_max_px=render_max_px,
                    pool=pool,
                )
            )
    else:
        results.append(
            ingest_one(
                db, filename=filename, content=content,
                uploaded_by=uploaded_by, render_max_px=render_max_px,
                pool=pool,
            )
        )

    return [asdict(r) for r in results]
