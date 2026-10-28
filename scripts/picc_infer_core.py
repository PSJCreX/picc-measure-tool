"""AI 추론 공통부 — 배치 생성과 요청 시 추론이 같은 코드를 쓴다.

왜 떼어냈나 — 전처리와 좌표 역변환이 두 곳에 복사돼 있으면, 한쪽만 고쳐져
**예측 좌표가 화면과 어긋나도 아무도 모른다.** 그건 조용히 틀리는 종류의 버그다.

모델 (2026-10-28 교체)
----------------------
논문의 최종 independent 모델 8개(`indep_<지표>.pt`)를 쓴다. 구조는 `model.py` 의
`KeypointNet` — EfficientNet-B0 백본에 stride 8/16/32 특징을 융합한 64×64
heatmap, spatial softmax 후 soft-argmax 로 연속 좌표를 낸다.

이전 세대 모델(`<지표>_detector.pth`, 사진 단위 분할)과 **규약이 다르다.**
혼동하면 조용히 틀리므로 적어 둔다:

* 점 개수 — 쇄골·2VBU 는 5점(옛 7점 + 0패딩), 나머지는 2점
* 거리 — 정렬 후 **첫 점과 마지막 점** 사이. 옛 코드의 `(0, 4)` 인덱스나
  2VBU 의 `existence > 0.5` 선별은 쓰지 않는다
* point-presence head 의 출력은 **추론에서 쓰지 않는다** (학습 보조 목표였다)
* 전처리 — modality LUT → (있으면) VOI LUT → MONOCHROME1 반전 →
  0.5–99.5 백분위 클리핑. 옛 min-max 정규화가 아니다

측정 기준 (2026-10-07 연구책임자 확정)
--------------------------------------
* 모델 로드 시간 제외 — 사람 쪽 `duration_ms` 도 사진이 뜬 뒤부터 센다
* 워밍업 3회 후 5회 중앙값 — 첫 추론은 커널 컴파일로 몇 배 느리다
* 픽셀 스페이싱은 **웹툴 DB 값**(`images.pixel_spacing_mm`) 을 쓴다.
  전문의 계측값이 그 값으로 mm 로 환산됐으므로, 모델 출력도 같은 값을 써야
  비교가 성립한다 (실제 영상 159장 전부 row = col 이고 DB 값과 일치한다).
"""
from __future__ import annotations

import os
import statistics
import time
from pathlib import Path

import numpy as np
import cv2
import pydicom
import torch
from pydicom.pixels import apply_modality_lut, apply_voi_lut
from pydicom.uid import ExplicitVRLittleEndian

from model import KeypointNet

TARGET = 512
WARMUP = 3
REPEAT = 5

# GPU 에서 bf16 autocast — 전달받은 참조 구현(`infer.py`)과 같게 둔다.
# PICC_BF16=0 으로 끌 수 있다.
BF16 = os.environ.get("PICC_BF16", "1") not in ("0", "false", "False")

# (DB 변수키, 체크포인트 지표명, 가중치파일, 점 정렬축)
# 정렬축 — 두 점의 순서를 영상 좌표 기준으로 일관되게 맞춘다. 길이는 대칭이라
# 어느 쪽이 먼저든 값은 같지만, 저장되는 `points` 순서가 흔들리지 않게 한다.
MODELS = [
    ("right_clavicle",    "Rt.CL",  "indep_Rt.CL.pt",  "x"),
    ("left_clavicle",     "Lt.CL",  "indep_Lt.CL.pt",  "x"),
    ("right_hvd",         "Rt.HVD", "indep_Rt.HVD.pt", "x"),
    ("left_hvd",          "Lt.HVD", "indep_Lt.HVD.pt", "x"),
    ("right_tcd",         "Rt.TCD", "indep_Rt.TCD.pt", "x"),
    ("left_tcd",          "Lt.TCD", "indep_Lt.TCD.pt", "x"),
    ("second_ribs_width", "2RHD",   "indep_2RHD.pt",   "x"),
    ("two_vbu",           "2VBU",   "indep_2VBU.pt",   "y"),
]

# 화면에 보일 이름 (progress 라벨용)
DISPLAY = {
    "right_clavicle": "Rt. Clavicle", "left_clavicle": "Lt. Clavicle",
    "right_hvd": "Rt. HVD", "left_hvd": "Lt. HVD",
    "right_tcd": "Rt. TCD", "left_tcd": "Lt. TCD",
    "second_ribs_width": "2nd Ribs Width", "two_vbu": "Two VBUs",
}


def device_info() -> tuple[str, str]:
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    name = torch.cuda.get_device_name(0) if dev == "cuda" else "cpu"
    return dev, name


def preprocess(dicom_path: Path):
    """논문 파이프라인과 **동일한** 전처리 + 좌표 역변환 정보.

    modality LUT → (있으면) VOI LUT → MONOCHROME1 반전 → 0.5–99.5 백분위
    클리핑 → 8비트 → 긴 변 512 비율 유지 리사이즈 → 대칭 zero-padding.
    역변환에 쓰려면 여기서 쓴 pad·scale 을 그대로 알아야 한다.
    """
    ds = pydicom.dcmread(str(dicom_path), force=True)
    if "TransferSyntaxUID" not in ds.file_meta:
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    a = ds.pixel_array
    if a.ndim == 3:
        a = a[..., 0] if a.shape[-1] in (3, 4) else a[0]
    oh, ow = a.shape[:2]

    a = apply_modality_lut(a, ds).astype(np.float32)
    try:
        if "VOILUTSequence" in ds or "WindowCenter" in ds:
            a = apply_voi_lut(a, ds).astype(np.float32)
    except Exception:                                  # noqa: BLE001
        pass
    if str(ds.get("PhotometricInterpretation", "")).upper() == "MONOCHROME1":
        a = a.max() - a

    lo, hi = np.percentile(a, [0.5, 99.5])
    a = np.clip((a - lo) / max(hi - lo, 1e-6), 0, 1)

    s = TARGET / max(oh, ow)
    nh, nw = round(oh * s), round(ow * s)
    r = cv2.resize(a, (nw, nh), interpolation=cv2.INTER_AREA)
    out = np.zeros((TARGET, TARGET), np.float32)
    pad_h, pad_w = (TARGET - nh) // 2, (TARGET - nw) // 2
    out[pad_h:pad_h + nh, pad_w:pad_w + nw] = r

    # 참조 구현이 8비트를 거쳐 다시 [0,1] 로 돌리므로 그대로 따른다.
    out = (out * 255).round().astype(np.uint8).astype(np.float32) / 255

    return out, {"orig_w": ow, "orig_h": oh, "pad_w": pad_w,
                 "pad_h": pad_h, "scale": max(ow, oh) / TARGET}


def to_original(x512: float, y512: float, geo: dict) -> list[float]:
    """512 패딩 좌표 → DICOM 원본 px. 계측(`Measurement.points`)과 같은 좌표계."""
    return [(x512 - geo["pad_w"]) * geo["scale"],
            (y512 - geo["pad_h"]) * geo["scale"]]


def load_models(models_dir: Path, device: str, on_step=None):
    """8개 모델을 올린다. `on_step(i, n, key)` 로 진행을 알린다.

    점 개수는 체크포인트의 `n_points` 를 그대로 믿는다 — 코드에 박아 두면
    가중치가 바뀔 때 조용히 어긋난다.
    """
    loaded = []
    n = len(MODELS)
    for i, (key, name, wfile, axis) in enumerate(MODELS, 1):
        if on_step:
            on_step(i, n, key)
        wpath = models_dir / wfile
        if not wpath.exists():
            continue
        ck = torch.load(wpath, map_location="cpu", weights_only=False)
        net = KeypointNet(ck["n_points"], pretrained=False)
        net.load_state_dict(ck["state_dict"])
        net.eval().to(device)
        loaded.append((key, net, axis, wfile))
    return loaded


def warmup(loaded, device: str, on_step=None) -> None:
    """커널 컴파일·캐시를 미리 치운다. 이 시간은 측정에 들어가지 않는다."""
    dummy = torch.zeros(1, 1, TARGET, TARGET, device=device)
    with torch.no_grad():
        for r in range(1, WARMUP + 1):
            if on_step:
                on_step(r, WARMUP)
            for _k, net, _a, _w in loaded:
                _forward(net, dummy, device)
    if device == "cuda":
        torch.cuda.synchronize()


def _forward(net, x, device: str):
    """좌표만 받는다 — presence head 출력은 쓰지 않는다."""
    with torch.autocast("cuda", dtype=torch.bfloat16,
                        enabled=BF16 and device == "cuda"):
        coords = net(x)[0]
    return coords.float()


def measure_one(net, x, axis, device: str, geo: dict, spacing: float | None):
    """한 항목 — 추론 시간(중앙값)과 원본 좌표·mm."""
    samples = []
    with torch.no_grad():
        for _ in range(REPEAT):
            if device == "cuda":
                torch.cuda.synchronize()
            t = time.perf_counter()
            coords = _forward(net, x, device)
            if device == "cuda":
                torch.cuda.synchronize()
            samples.append((time.perf_counter() - t) * 1000)
    ms = statistics.median(samples)

    kp = coords[0].cpu().numpy()                       # (K, 2), 0–512 좌표
    k = 0 if axis == "x" else 1
    if kp[0, k] > kp[-1, k]:
        kp = kp[::-1]
    pts = [to_original(float(kp[i][0]), float(kp[i][1]), geo) for i in (0, -1)]
    d = float(np.hypot(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]))
    return {"points": pts, "value_mm": (d * spacing if spacing else None),
            "infer_ms": ms}
