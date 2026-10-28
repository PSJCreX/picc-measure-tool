"""AI 예측을 생성해 `predictions` 테이블에 저장한다.

모델·전처리·좌표 역변환은 **전부 `picc_infer_core` 에서 가져온다.** 예전에는 이
파일이 같은 로직을 따로 들고 있었고, 그래서 한쪽만 고쳐지면 배치 예측과 요청 시
예측이 조용히 어긋날 수 있었다. 지금은 정의가 한 곳뿐이다.

측정 기준 (2026-10-07 연구책임자 확정)
--------------------------------------
* **모델 로드 시간은 제외한다.** 사람 쪽 `duration_ms` 도 사진이 뜬 뒤부터
  마지막 점을 놓기까지만 센 값이므로, 가중치를 메모리에 올리는 시간은 비교 대상이
  아니다. 모델은 프로세스 시작에 한 번만 로드한다.
* **워밍업 3회 후 5회의 중앙값.** 첫 추론은 CUDA 커널 컴파일·캐시 때문에 몇 배
  느리다. 그 값을 쓰면 모델을 부당하게 느리게 보고하는 셈이다.
* **픽셀 스페이싱은 웹툴 DB 값**(`images.pixel_spacing_mm`)을 쓴다. 전문의 계측값이
  그 값으로 mm 로 환산됐으므로 모델 출력도 같은 값을 써야 비교가 성립한다.

전처리 시간(`preprocess_ms`)은 **따로** 저장한다 — 논문의 대표 숫자는 순수 추론
시간이지만, "영상을 읽는 것까지 포함하면" 을 나중에 묻게 될 수 있다. 다시 돌리지
않아도 되게 둘 다 남긴다.

실행 (Spark)
    cd ~/picc-infer && ./.venv/bin/python src/generate_predictions.py            # 전체
    cd ~/picc-infer && ./.venv/bin/python src/generate_predictions.py --limit 3  # 맛보기
    ...                                                        --image-id 169    # 한 장만
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

INFER_ROOT = Path(__file__).resolve().parent
MODELS_DIR = INFER_ROOT.parent / "models"
# 웹툴의 data 폴더 — .env 의 DATA_DIR 과 같은 곳을 가리켜야 한다.
WEB_DATA = Path(os.environ.get("PICC_WEB_DATA", "")
                or Path(__file__).resolve().parents[1] / "data")
DB = WEB_DATA / "app.db"

sys.path.insert(0, str(INFER_ROOT))

import torch                                          # noqa: E402
from picc_infer_core import (                         # noqa: E402
    REPEAT, WARMUP, device_info, load_models, measure_one,
    preprocess, warmup,
)


def main() -> int:
    """`--json` 이면 stdout 은 JSON 만 내보낸다 — 웹 엔드포인트가 파싱하므로
    사람용 진행 메시지는 stderr 로 돌린다."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--image-id", type=int, default=None, help="한 장만")
    ap.add_argument("--limit", type=int, default=None, help="앞 N 장만")
    ap.add_argument("--force", action="store_true", help="이미 있는 예측도 다시 계산")
    ap.add_argument("--dry-run", action="store_true", help="저장하지 않고 출력만")
    ap.add_argument("--json", action="store_true",
                    help="결과를 JSON 으로 stdout 에 — 웹 엔드포인트가 쓴다")
    args = ap.parse_args()

    dev, dev_name = device_info()
    log = (lambda *a, **k: print(*a, file=sys.stderr, **k)) if args.json else print
    log(f"장치: {dev} ({dev_name})  torch {torch.__version__}")
    if dev == "cuda":
        import subprocess
        util = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu",
                               "--format=csv,noheader"],
                              capture_output=True, text=True).stdout.strip()
        log(f"GPU 사용률(시작 전): {util}  ← 경합 중이면 시간이 부풀려진다")

    # 대상 영상 — 실제 계측 pool 만 (연습은 논문 대상이 아니다)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    q = ("SELECT id, dicom_path, pixel_spacing_mm, original_filename FROM images"
         " WHERE status='active' AND pool='real'")
    params: tuple = ()
    if args.image_id is not None:
        q += " AND id=?"; params = (args.image_id,)
    q += " ORDER BY id"
    rows = con.execute(q, params).fetchall()
    done_pairs = set()
    if not args.force:
        try:
            done_pairs = {(r[0], r[1]) for r in con.execute(
                "SELECT image_id, variable_key FROM predictions").fetchall()}
        except sqlite3.OperationalError:
            pass          # 테이블이 아직 없다 — 첫 실행
    con.close()
    if args.limit:
        rows = rows[:args.limit]
    log(f"대상 영상 {len(rows)}장, 이미 예측된 (영상×항목) {len(done_pairs)}건")
    if not rows:
        log("할 일이 없습니다."); return 0

    # DB 백업 — 연구 데이터가 든 파일에 쓰기 전에
    if not args.dry_run:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bdir = WEB_DATA / "backups"; bdir.mkdir(exist_ok=True)
        bk = bdir / f"app_{stamp}_before_predictions.db"
        src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        dst = sqlite3.connect(bk)
        src.backup(dst); dst.close(); src.close()
        log(f"DB 백업: {bk.name} ({bk.stat().st_size} bytes)")

    log("\n모델 로드 (측정에서 제외)")
    t0 = time.perf_counter()
    loaded = load_models(MODELS_DIR, dev,
                         on_step=lambda i, n, key: log(f"  {i}/{n} {key}"))
    log(f"  {len(loaded)}개 / {time.perf_counter() - t0:.1f}s")
    if not loaded:
        log("쓸 수 있는 모델이 없습니다."); return 1

    # 전역 워밍업 — 커널 컴파일은 프로세스에 한 번이면 된다
    warmup(loaded, dev)
    log(f"  워밍업 {WARMUP}회 완료\n")

    wcon = None if args.dry_run else sqlite3.connect(DB, timeout=30)
    if wcon:
        wcon.execute("PRAGMA busy_timeout=30000")

    n_img = n_pred = 0
    collected: dict[int, list[dict]] = {}
    t_all = time.perf_counter()
    for img_id, dicom_rel, spacing, fname in rows:
        todo = [x for x in loaded if args.force or (img_id, x[0]) not in done_pairs]
        if not todo:
            continue

        tp = time.perf_counter()
        padded, geo = preprocess(WEB_DATA / dicom_rel)
        pre_ms = (time.perf_counter() - tp) * 1000
        x = torch.from_numpy(padded)[None, None].to(dev)

        per_item = []
        for key, net, axis, wfile in todo:
            r = measure_one(net, x, axis, dev, geo, spacing)
            per_item.append((key, r["points"], r["value_mm"], r["infer_ms"], wfile))
            collected.setdefault(img_id, []).append({
                "variable_key": key, "points": r["points"],
                "value_mm": r["value_mm"], "infer_ms": r["infer_ms"],
                "preprocess_ms": pre_ms,
            })
            n_pred += 1

        if wcon:
            for key, pts, mm, ms, wfile in per_item:
                wcon.execute("""
                    INSERT INTO predictions
                      (image_id, variable_key, points, value_mm, infer_ms,
                       preprocess_ms, device, device_name, torch_version,
                       model_file, notes, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                    ON CONFLICT(image_id, variable_key) DO UPDATE SET
                      points=excluded.points, value_mm=excluded.value_mm,
                      infer_ms=excluded.infer_ms, preprocess_ms=excluded.preprocess_ms,
                      device=excluded.device, device_name=excluded.device_name,
                      torch_version=excluded.torch_version,
                      model_file=excluded.model_file, created_at=datetime('now')
                """, (img_id, key, json.dumps(pts), mm, ms, pre_ms, dev, dev_name,
                      torch.__version__, wfile,
                      f"warmup={WARMUP} repeat={REPEAT} median; spacing from web DB"))
            wcon.commit()

        n_img += 1
        tot = sum(p[3] for p in per_item)
        log(f"  #{img_id:<4} {fname[:22]:22s} {len(per_item)}항목 "
            f"추론 {tot:6.1f}ms  전처리 {pre_ms:6.1f}ms")

    if wcon:
        wcon.close()
    el = time.perf_counter() - t_all
    log(f"\n영상 {n_img}장 / 예측 {n_pred}건 / 총 {el:.1f}s"
        f"{' (dry-run — 저장 안 함)' if args.dry_run else ''}")
    if args.json:
        print(json.dumps({
            "ok": True,
            "device": dev, "device_name": dev_name,
            "torch_version": torch.__version__,
            "warmup": WARMUP, "repeat": REPEAT,
            "images": collected,
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
