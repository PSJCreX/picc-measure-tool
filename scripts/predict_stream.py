"""영상 한 장을 요청 시 추론하고, **진행 상황을 한 줄씩 보고**한다.

웹 화면의 progress bar 가 실제 단계를 그대로 보여주도록 만든 것이다 —
모델 로드 중 / 워밍업 (1/3) / 측정 중 (Rt. Clavicle) … 처럼.

왜 표준출력에 접두어를 붙이나
-----------------------------
검출 모듈들이 import·초기화 시점에 자기 메시지("✓ EfficientNet 백본 …")를
stdout 으로 찍는다. 그 소음과 섞이면 웹쪽이 JSON 을 파싱하지 못한다. 그래서
우리 줄만 `@@P@@`(진행) / `@@R@@`(결과) 로 표시하고, 웹은 그 줄만 집어낸다.

표준오류는 사람이 보는 로그로 남겨 둔다.

실행
    ./.venv/bin/python src/predict_stream.py --image-id 169
    ./.venv/bin/python src/predict_stream.py --image-id 169 --no-store
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODELS_DIR = HERE.parent / "models"
# 웹툴의 data 폴더 — .env 의 DATA_DIR 과 같은 곳을 가리켜야 한다.
WEB_DATA = Path(os.environ.get("PICC_WEB_DATA", "")
                or Path(__file__).resolve().parents[1] / "data")
DB = WEB_DATA / "app.db"

sys.path.insert(0, str(HERE))

import torch                                          # noqa: E402
from picc_infer_core import (                         # noqa: E402
    DISPLAY, REPEAT, WARMUP, device_info, load_models, measure_one,
    preprocess, warmup,
)

P, R = "@@P@@ ", "@@R@@ "


def emit(stage: str, **kw) -> None:
    """진행 한 줄. 즉시 flush 해야 화면이 실시간으로 움직인다."""
    sys.stdout.write(P + json.dumps({"stage": stage, **kw}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image-id", type=int, required=True)
    ap.add_argument("--no-store", action="store_true", help="DB 에 저장하지 않음")
    args = ap.parse_args()

    t_wall = time.perf_counter()
    dev, dev_name = device_info()
    emit("start", device=dev, device_name=dev_name,
         torch=torch.__version__, warmup=WARMUP, repeat=REPEAT)

    # 영상 경로와 **올바른** 픽셀 스페이싱 (웹툴이 해석해 둔 값)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    row = con.execute(
        "SELECT dicom_path, pixel_spacing_mm, spacing_source, original_filename"
        " FROM images WHERE id=?", (args.image_id,)).fetchone()
    con.close()
    if row is None:
        sys.stdout.write(R + json.dumps({"ok": False, "error": "영상을 찾을 수 없습니다."}) + "\n")
        return 1
    dicom_rel, spacing, spacing_src, fname = row

    # 1) 모델 로드 — 측정에서 제외되는 구간이지만 사용자는 기다리므로 보여준다
    emit("load_begin", n=8)
    t0 = time.perf_counter()
    loaded = load_models(
        MODELS_DIR, dev,
        on_step=lambda i, n, key: emit("load", i=i, n=n, key=key,
                                       label=DISPLAY.get(key, key)),
    )
    load_ms = (time.perf_counter() - t0) * 1000
    emit("load_done", n=len(loaded), ms=round(load_ms, 1))
    if not loaded:
        sys.stdout.write(R + json.dumps({"ok": False, "error": "모델 가중치가 없습니다."}) + "\n")
        return 1

    # 2) 전처리 — DICOM 로드 + 512 리사이즈·패딩 (영상당 한 번)
    emit("preprocess_begin")
    tp = time.perf_counter()
    padded, geo = preprocess(WEB_DATA / dicom_rel)
    pre_ms = (time.perf_counter() - tp) * 1000
    x = torch.from_numpy(padded)[None, None].to(dev)
    emit("preprocess_done", ms=round(pre_ms, 1),
         size=f"{geo['orig_w']}x{geo['orig_h']}")

    # 3) 워밍업 — 첫 추론의 커널 컴파일을 미리 치운다(측정에서 제외)
    emit("warmup_begin", n=WARMUP)
    warmup(loaded, dev, on_step=lambda r, n: emit("warmup", i=r, n=n))
    emit("warmup_done", n=WARMUP)

    # 4) 측정 — 항목별 REPEAT 회 중앙값
    emit("measure_begin", n=len(loaded))
    results = []
    for i, (key, net, idx, wfile) in enumerate(loaded, 1):
        emit("measure", i=i, n=len(loaded), key=key,
             label=DISPLAY.get(key, key))
        r = measure_one(net, x, idx, dev, geo, spacing)
        r.update(variable_key=key, label=DISPLAY.get(key, key),
                 preprocess_ms=pre_ms, model_file=wfile)
        results.append(r)
        emit("measured", i=i, n=len(loaded), key=key,
             label=DISPLAY.get(key, key),
             ms=round(r["infer_ms"], 2),
             value_mm=(round(r["value_mm"], 1) if r["value_mm"] is not None else None))

    total_ms = sum(r["infer_ms"] for r in results)

    # 5) 저장 — 캐시 겸 논문 기록. 같은 (영상, 항목)은 덮어쓴다
    stored = False
    if not args.no_store:
        emit("store_begin")
        try:
            w = sqlite3.connect(DB, timeout=30)
            w.execute("PRAGMA busy_timeout=30000")
            for r in results:
                w.execute("""
                    INSERT INTO predictions
                      (image_id, variable_key, points, value_mm, infer_ms,
                       preprocess_ms, device, device_name, torch_version,
                       model_file, notes, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                    ON CONFLICT(image_id, variable_key) DO UPDATE SET
                      points=excluded.points, value_mm=excluded.value_mm,
                      infer_ms=excluded.infer_ms,
                      preprocess_ms=excluded.preprocess_ms,
                      device=excluded.device, device_name=excluded.device_name,
                      torch_version=excluded.torch_version,
                      model_file=excluded.model_file, created_at=datetime('now')
                """, (args.image_id, r["variable_key"], json.dumps(r["points"]),
                      r["value_mm"], r["infer_ms"], pre_ms, dev, dev_name,
                      torch.__version__, r["model_file"],
                      f"warmup={WARMUP} repeat={REPEAT} median; "
                      f"spacing={spacing} ({spacing_src}) from web DB"))
            w.commit(); w.close()
            stored = True
        except Exception as exc:                      # noqa: BLE001
            emit("store_failed", error=str(exc))
        if stored:
            emit("store_done", n=len(results))

    sys.stdout.write(R + json.dumps({
        "ok": True,
        "image_id": args.image_id,
        "filename": fname,
        "device": dev, "device_name": dev_name,
        "torch_version": torch.__version__,
        "warmup": WARMUP, "repeat": REPEAT,
        "spacing_mm": spacing, "spacing_source": spacing_src,
        "load_ms": round(load_ms, 1),
        "preprocess_ms": round(pre_ms, 1),
        "total_infer_ms": round(total_ms, 2),
        "wall_ms": round((time.perf_counter() - t_wall) * 1000, 1),
        "stored": stored,
        "results": [{
            "variable_key": r["variable_key"],
            "label": r["label"],
            "points": r["points"],
            "value_mm": r["value_mm"],
            "infer_ms": r["infer_ms"],
        } for r in results],
    }, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
