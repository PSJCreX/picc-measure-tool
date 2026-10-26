"""연구용 데이터 내보내기 — XLSX / CSV / ZIP.

계측의 단위가 (영상 × 변수) 문제 하나이므로, 소요시간도 그 단위로 나온다.
가설 2(속도)는 이 `duration_ms`를 AI의 항목별 추론 시간과 맞대어 검증한다.

산출물
------
1. measurements_wide.xlsx  — 행=영상. 계측자별 8개 값·소요시간·확정값을 옆으로 펼침
2. measurements_long.csv   — 행=(영상×계측자×변수). R/SPSS 직행용 tidy 포맷
3. disagreements.xlsx      — 허용범위를 넘은 (영상, 변수)만
4. annotations_bundle.zip  — 확정 GT 좌표를 학습용 JSON 포맷으로
5. full_bundle.zip         — 위 전부 + 원본 DICOM + 렌더 PNG

ZIP은 제너레이터로 스트리밍한다(ABDXAI `export_bundle` 방식). 519장 × 14MB면
7GB가 넘어서 메모리에 다 올릴 수 없다.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Iterator

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agreement import (
    KIND_MISMATCH,
    active_reviewers,
    classify_all,
    current_measurements,
)
from app.config import DATA_DIR
from app.models import M_DONE, POOL_REAL, Image, Measurement
from app.variables import ALL_VARIABLE_KEYS, VARIABLES

_SPACING_LABELS = {
    "PixelSpacing": "PixelSpacing(0028,0030)",
    "ImagerPixelSpacing": "ImagerPixelSpacing(0018,1164)",
    "manual": "수동입력",
    "unknown": "미상",
}


def _active_images(db: Session) -> list[Image]:
    """내보내기 대상 영상 — **실제 계측 pool만**.

    연습(`pool='practice'`) 영상과 그 계측은 어떤 산출물에도 들어가지 않는다.
    아래 네 빌더가 전부 이 함수를 거치고, 계측은 `images` 사전에 없는 영상이면
    건너뛰므로 여기 한 곳만 막으면 연습 기록이 새어 나갈 길이 없다.
    """
    return list(
        db.scalars(
            select(Image)
            .where(Image.status == "active", Image.pool == POOL_REAL)
            .order_by(Image.id)
        ).all()
    )


def _round(v: float | None, nd: int = 2) -> float | str:
    return "" if v is None else round(float(v), nd)


# ----------------------------------------------------------------------------
# 1. wide
# ----------------------------------------------------------------------------


def build_wide_frame(db: Session, tolerance_mm: float) -> pd.DataFrame:
    images = _active_images(db)
    reviewers = active_reviewers(db)
    per_image = current_measurements(db, [im.id for im in images])
    agreements = classify_all(db, images, tolerance_mm)

    rows: list[dict] = []
    for im in images:
        agr = agreements[im.id]
        row: dict = {
            "영상ID": im.id,
            "파일명": im.original_filename,
            "원본크기": f"{im.orig_w}x{im.orig_h}",
            "픽셀스페이싱(mm)": _round(im.pixel_spacing_mm, 6),
            "스페이싱출처": _SPACING_LABELS.get(im.spacing_source, im.spacing_source),
            "행판정": agr.kind,
            "계측진행": f"{agr.n_measured}/{agr.n_expected}",
        }

        for u in reviewers:
            label = u.username
            mine = per_image.get(im.id, {}).get(int(u.id), {})
            for k in ALL_VARIABLE_KEYS:
                m = mine.get(k)
                row[f"[{label}] {VARIABLES[k].short}(mm)"] = _round(
                    m.value_mm if m else None
                )
            for k in ALL_VARIABLE_KEYS:
                m = mine.get(k)
                row[f"[{label}] {VARIABLES[k].short} 소요(ms)"] = (
                    m.duration_ms if m and m.duration_ms is not None else ""
                )

        # 변수별 차이(spread) — 불일치 판단 근거를 표에 남긴다
        for k in ALL_VARIABLE_KEYS:
            va = agr.per_variable.get(k)
            row[f"차이 {VARIABLES[k].short}(mm)"] = _round(
                va.spread_mm if va else None
            )

        rows.append(row)

    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# 2. long (tidy) — 통계 패키지 직행용
# ----------------------------------------------------------------------------


def build_long_frame(db: Session, tolerance_mm: float) -> pd.DataFrame:
    """행 = (영상 × 계측자 × 변수). 재측정 요청으로 버린 시도도 함께 내보낸다.

    `status` 열로 걸러 쓸 수 있게 했다 — 유효 계측만 보려면 `status == 'done'`.
    버린 시도의 소요시간은 속도 분석에서 빼야 한다.
    """
    images = {im.id: im for im in _active_images(db)}
    reviewers = {int(u.id): u for u in active_reviewers(db)}
    agreements = classify_all(db, list(images.values()), tolerance_mm)

    rows: list[dict] = []
    all_measurements = db.scalars(
        select(Measurement).order_by(Measurement.image_id, Measurement.id)
    ).all()

    for m in all_measurements:
        im = images.get(m.image_id)
        u = reviewers.get(m.user_id)
        if im is None or u is None:
            continue
        agr = agreements.get(im.id)
        va = agr.per_variable.get(m.variable_key) if agr else None
        rows.append({
            "image_id": im.id,
            "filename": im.original_filename,
            "pixel_spacing_mm": im.pixel_spacing_mm,
            "spacing_source": im.spacing_source,
            "rater": u.username,
            "rater_name": u.display_label(),
            "variable": m.variable_key,
            "variable_short": VARIABLES[m.variable_key].short
                              if m.variable_key in VARIABLES else m.variable_key,
            "value_mm": m.value_mm,
            "value_cm": None if m.value_mm is None else m.value_mm / 10.0,
            # 사진이 뜬 순간 → 마지막 drop. 가설 2(속도)의 사람 쪽 숫자.
            "duration_ms": m.duration_ms,
            "attempt": m.attempt,
            "status": m.status,
            "measured_at": m.created_at.isoformat() if m.created_at else None,
            "agreement_kind": va.kind if va else "",
            "spread_mm": va.spread_mm if va else None,
            "tolerance_mm": tolerance_mm,
        })
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# 3. 불일치 목록
# ----------------------------------------------------------------------------


def build_disagreement_frame(db: Session, tolerance_mm: float) -> pd.DataFrame:
    images = _active_images(db)
    reviewers = {int(u.id): u for u in active_reviewers(db)}
    agreements = classify_all(db, images, tolerance_mm)
    per_image = current_measurements(db, [im.id for im in images])

    rows: list[dict] = []
    for im in images:
        agr = agreements[im.id]
        for k, va in agr.per_variable.items():
            if va.kind != KIND_MISMATCH:
                continue
            row = {
                "영상ID": im.id,
                "파일명": im.original_filename,
                "변수": VARIABLES[k].display,
                "차이(mm)": _round(va.spread_mm),
                "허용범위(mm)": tolerance_mm,
                "평균(mm)": _round(va.mean_mm),
            }
            for uid, u in reviewers.items():
                row[f"[{u.username}](mm)"] = _round(va.values.get(uid))
                m = per_image.get(im.id, {}).get(uid, {}).get(k)
                row[f"[{u.username}] 소요(ms)"] = (
                    m.duration_ms if m and m.duration_ms is not None else ""
                )
            rows.append(row)

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("차이(mm)", ascending=False, ignore_index=True)
    return df


# ----------------------------------------------------------------------------
# 4. 학습용 어노테이션 JSON 번들
# ----------------------------------------------------------------------------


def annotation_json_files(db: Session) -> Iterator[tuple[str, bytes]]:
    """**계측자별** 좌표를 기존 학습 어노테이션 포맷으로 내보낸다.

    확정값(GT)을 만들지 않으므로 '정답 한 벌'이 나오지 않는다. 대신 계측자마다
    한 벌씩, 각자가 실제로 찍은 좌표를 그대로 낸다.

        annotations/<계측자>/<항목>/<파일명>_<항목>.json

    포맷은 `data/annotations/<target>/<base>_<target>.json`과 동일하다
    (clavicle/two_vbu는 `points`, hvd/tcd/2nd ribs는 `line`). 좌표계도 같은
    **DICOM 원본 픽셀**이라 계측자 한 명분을 골라 그대로 학습에 쓸 수 있다.

    `status='done'`인 현재 계측만 대상이다 — 재측정 요청된 시도(`redo`)는 뺀다.
    """
    images = {im.id: im for im in _active_images(db)}
    reviewers = {u.id: u for u in active_reviewers(db)}
    stamp = datetime.utcnow().isoformat()

    rows = db.scalars(
        select(Measurement)
        .where(Measurement.status == M_DONE)
        .order_by(Measurement.image_id, Measurement.id)
    ).all()

    for m in rows:
        im = images.get(m.image_id)
        u = reviewers.get(m.user_id)
        var = VARIABLES.get(m.variable_key)
        if im is None or u is None or var is None or not m.points:
            continue
        base = Path(im.original_filename).stem
        payload = {
            "image_path": f"data/dicom_images/{base}.dcm",
            var.ann_shape: m.points,
            "dicom_original_width": im.orig_w,
            "dicom_original_height": im.orig_h,
            "value_mm": m.value_mm,
            "pixel_spacing_mm": im.pixel_spacing_mm,
            "rater": u.username,
            "duration_ms": m.duration_ms,
            "attempt": m.attempt,
            "saved_at": stamp,
        }
        data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        yield f"annotations/{u.username}/{m.variable_key}/{base}_{m.variable_key}.json", data


# ----------------------------------------------------------------------------
# 스트리밍 헬퍼
# ----------------------------------------------------------------------------


def xlsx_bytes(frames: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for sheet, df in frames.items():
            (df if not df.empty else pd.DataFrame([{"결과": "해당 없음"}])).to_excel(
                writer, index=False, sheet_name=sheet[:31]
            )
    buf.seek(0)
    return buf.getvalue()


def csv_bytes(df: pd.DataFrame) -> bytes:
    # utf-8-sig — 엑셀에서 바로 열어도 한글이 깨지지 않는다
    return df.to_csv(index=False).encode("utf-8-sig")


class _ZipStreamBuf(io.RawIOBase):
    """ZipFile이 쓴 바이트를 조각으로 모으되, `tell()`은 **전체 스트림 기준**으로 센다.

    왜 필요한가 — 예전에는 `io.BytesIO`를 쓰고 조각을 흘려보낸 뒤 `truncate(0)`로
    비웠다. 그러면 `tell()`이 0으로 되돌아가는데, ZIP 중앙목록에 기록되는
    각 멤버의 오프셋이 바로 이 `tell()` 값이다. 결과로 나온 ZIP은
    **목록은 보이지만 멤버를 하나도 못 여는** 상태가 된다
    (`BadZipFile: Bad magic number for file header`).

    그래서 버퍼를 비워도 위치는 계속 누적한다. 메모리에는 아직 흘려보내지 않은
    조각만 남으므로 큰 DICOM 번들도 파일 하나 분량만 올라간다.
    """

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._pos = 0

    def write(self, b) -> int:                      # noqa: D102
        data = bytes(b)
        self._chunks.append(data)
        self._pos += len(data)
        return len(data)

    def tell(self) -> int:
        return self._pos

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        # False → zipfile이 data descriptor 방식으로 쓴다(스트리밍 ZIP의 정석).
        return False

    def drain(self) -> bytes:
        out = b"".join(self._chunks)
        self._chunks.clear()
        return out


def stream_bundle(
    db: Session, tolerance_mm: float, include_media: bool
) -> Iterator[bytes]:
    """ZIP을 조각내어 흘려보낸다. 한 번에 파일 하나씩만 메모리에 올린다."""
    buf = _ZipStreamBuf()
    zf = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6)

    def drain() -> bytes:
        return buf.drain()

    wide = build_wide_frame(db, tolerance_mm)
    long = build_long_frame(db, tolerance_mm)
    dis = build_disagreement_frame(db, tolerance_mm)

    zf.writestr("measurements_wide.xlsx", xlsx_bytes({"measurements": wide}))
    zf.writestr("measurements_long.csv", csv_bytes(long))
    zf.writestr("disagreements.xlsx", xlsx_bytes({"disagreements": dis}))
    zf.writestr(
        "README.txt",
        (
            "PICC 계측 라벨링 내보내기\n"
            f"생성 시각(UTC): {datetime.utcnow().isoformat()}\n"
            f"허용범위(mm): {tolerance_mm}\n\n"
            "계측 단위는 (영상 x 변수) 문제 하나이며,\n"
            "duration_ms 는 '사진이 뜬 순간 ~ 마지막 drop' 구간이다.\n"
            "문제를 읽는 시간과 확정을 고민한 시간은 포함하지 않는다.\n\n"
            "measurements_wide.xlsx  행=영상, 계측자별 값을 옆으로 펼침\n"
            "measurements_long.csv   행=(영상x계측자x변수), 통계 패키지용\n"
            "                        status='done' 만 유효. 'redo'는 버려진 시도.\n"
            "disagreements.xlsx      허용범위 초과 항목\n"
            "annotations/            확정 GT 좌표(학습 포맷 JSON)\n"
            "dicom/, png/            원본 및 렌더 영상\n"
        ).encode("utf-8"),
    )
    for name, data in annotation_json_files(db):
        zf.writestr(name, data)
    yield drain()

    if include_media:
        for im in _active_images(db):
            for rel, folder in ((im.dicom_path, "dicom"), (im.png_path, "png")):
                src = DATA_DIR / rel
                if not src.exists():
                    continue
                arc = f"{folder}/{Path(im.original_filename).stem}{src.suffix}"
                zf.write(src, arcname=arc)
                chunk = drain()
                if chunk:
                    yield chunk

    zf.close()
    tail = drain()
    if tail:
        yield tail
