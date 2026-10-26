"""AI 예측을 생성해 `predictions` 테이블에 저장한다.

측정 기준 (2026-10-07 연구책임자 확정)
--------------------------------------
* **모델 로드 시간은 제외한다.** 사람 쪽 `duration_ms` 도 사진이 뜬 뒤부터
  마지막 점을 놓기까지만 센 값이므로, 가중치를 메모리에 올리는 시간은 비교 대상이
  아니다. 모델은 프로세스 시작에 한 번만 로드한다.
* **워밍업 3회 후 5회의 중앙값.** 첫 추론은 CUDA 커널 컴파일·캐시 때문에 몇 배
  느리다. 그 값을 쓰면 모델을 부당하게 느리게 보고하는 셈이다.
* **픽셀 스페이싱은 웹툴 DB 값**(`images.pixel_spacing_mm`)을 쓴다. 추론 코드의
  `load_dicom_image` 는 `PixelSpacing` 만 읽고 없으면 0.2mm 로 고정하는데,
  test set 의 약 23% 가 `ImagerPixelSpacing` 만 갖고 있다. 그 폴백을 쓰면 오차가
  모델 성능이 아니라 단위 환산에서 나온다.

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
import shutil
import sqlite3
import statistics
import os
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

import numpy as np                                    # noqa: E402
import cv2                                            # noqa: E402
import pydicom                                        # noqa: E402
import torch                                          # noqa: E402

MODELS = [
    ("right_clavicle",    "right_clavicle_detection",    "RightClavicleKeypointDetector",   "right_clavicle_detector.pth",    7, (0, 4)),
    ("left_clavicle",     "left_clavicle_detection",     "LeftClavicleKeypointDetector",    "left_clavicle_detector.pth",     7, (0, 4)),
    ("right_hvd",         "right_hvd_detection",         "RightHVDKeypointDetector",        "right_hvd_detector.pth",         2, (0, 1)),
    ("left_hvd",          "left_hvd_detection",          "LeftHVDKeypointDetector",         "left_hvd_detector.pth",          2, (0, 1)),
    ("right_tcd",         "right_tcd_detection",         "RightTCDKeypointDetector",        "right_tcd_detector.pth",         2, (0, 1)),
    ("left_tcd",          "left_tcd_detection",          "LeftTCDKeypointDetector",         "left_tcd_detector.pth",          2, (0, 1)),
    ("second_ribs_width", "second_ribs_width_detection", "SecondRibsWidthKeypointDetector", "second_ribs_width_detector.pth", 2, (0, 1)),
    ("two_vbu",           "two_vbu_detection",           "TwoVBUKeypointDetector",          "two_vbu_detector.pth",           7, None),
]

TARGET = 512
WARMUP = 3
REPEAT = 5


def preprocess(dicom_path: Path):
    """추론 코드(`load_dicom_image`)와 동일한 전처리. 역변환 정보도 돌려준다."""
    ds = pydicom.dcmread(str(dicom_path))
    image = ds.pixel_array
    oh, ow = image.shape[:2]

    image = image.astype(np.float32)
    rng = image.max() - image.min()
    image = (image - image.min()) / (rng if rng else 1.0)

    if ow > oh:
        new_w, new_h = TARGET, int(oh * TARGET / ow)
    else:
        new_h, new_w = TARGET, int(ow * TARGET / oh)

    resized = cv2.resize(image, (new_w, new_h))
    padded = np.zeros((TARGET, TARGET), dtype=np.float32)
    pad_h = (TARGET - new_h) // 2
    pad_w = (TARGET - new_w) // 2
    padded[pad_h:pad_h + new_h, pad_w:pad_w + new_w] = resized

    return padded, {"orig_w": ow, "orig_h": oh, "pad_w": pad_w, "pad_h": pad_h,
                    "scale": max(ow, oh) / TARGET}


def to_original(x512: float, y512: float, geo: dict) -> list[float]:
    """512 패딩 좌표 → DICOM 원본 px (계측과 같은 좌표계)."""
    return [(x512 - geo["pad_w"]) * geo["scale"],
            (y512 - geo["pad_h"]) * geo["scale"]]


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

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    dev_name = torch.cuda.get_device_name(0) if dev == "cuda" else "cpu"
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
    loaded = []
    for key, mod_name, cls_name, wfile, maxp, idx in MODELS:
        wpath = MODELS_DIR / wfile
        if not wpath.exists():
            log(f"  X {key}: 가중치 없음 — 건너뜀"); continue
        mod = __import__(mod_name)
        net = getattr(mod, cls_name)(max_points=maxp, use_pretrained=False)
        st = torch.load(wpath, map_location="cpu", weights_only=False)
        net.load_state_dict(st["model_state_dict"] if "model_state_dict" in st else st)
        net.eval().to(dev)
        loaded.append((key, net, idx, wfile))
    log(f"  {len(loaded)}개 / {time.perf_counter() - t0:.1f}s")
    if not loaded:
        log("쓸 수 있는 모델이 없습니다."); return 1

    # 전역 워밍업 — 커널 컴파일은 프로세스에 한 번이면 된다
    dummy = torch.zeros(1, 1, TARGET, TARGET, device=dev)
    with torch.no_grad():
        for _ in range(WARMUP):
            for _k, net, _i, _w in loaded:
                net(dummy)
    if dev == "cuda":
        torch.cuda.synchronize()
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
        for key, net, idx, wfile in todo:
            samples = []
            with torch.no_grad():
                for _ in range(REPEAT):
                    if dev == "cuda":
                        torch.cuda.synchronize()
                    t1 = time.perf_counter()
                    out = net(x)
                    if dev == "cuda":
                        torch.cuda.synchronize()
                    samples.append((time.perf_counter() - t1) * 1000)
            ms = statistics.median(samples)

            kp = out["keypoints"][0].detach().cpu().numpy()
            ex = out["existence_probs"][0].detach().cpu().numpy()
            if idx is None:                     # two_vbu — 유효 점의 첫/마지막
                valid = [i for i in range(len(ex)) if ex[i] > 0.5]
                pick = (valid[0], valid[-1]) if len(valid) >= 2 else (0, 1)
            else:
                pick = idx
            pts = [to_original(float(kp[i][0]), float(kp[i][1]), geo) for i in pick]
            d = float(np.hypot(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]))
            mm = d * spacing if spacing else None

            per_item.append((key, pts, mm, ms, wfile))
            collected.setdefault(img_id, []).append({
                "variable_key": key, "points": pts, "value_mm": mm,
                "infer_ms": ms, "preprocess_ms": pre_ms,
            })
            n_pred += 1

        if wcon:
            import json as _json
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
                """, (img_id, key, _json.dumps(pts), mm, ms, pre_ms, dev, dev_name,
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
        import json as _json
        print(_json.dumps({
            "ok": True,
            "device": dev, "device_name": dev_name,
            "torch_version": torch.__version__,
            "warmup": WARMUP, "repeat": REPEAT,
            "images": collected,
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
