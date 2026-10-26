"""AI 추론 공통부 — 배치 생성과 요청 시 추론이 같은 코드를 쓴다.

왜 떼어냈나 — 전처리와 좌표 역변환이 두 곳에 복사돼 있으면, 한쪽만 고쳐져
**예측 좌표가 화면과 어긋나도 아무도 모른다.** 그건 조용히 틀리는 종류의 버그다.

측정 기준 (2026-10-07 연구책임자 확정)
--------------------------------------
* 모델 로드 시간 제외 — 사람 쪽 `duration_ms` 도 사진이 뜬 뒤부터 센다
* 워밍업 3회 후 5회 중앙값 — 첫 추론은 커널 컴파일로 몇 배 느리다
* 픽셀 스페이싱은 웹툴 DB 값(`images.pixel_spacing_mm`) — 추론 코드의
  `PixelSpacing`-only 폴백(없으면 0.2mm)은 test set 의 약 23% 를 잘못 환산한다
"""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

import numpy as np
import cv2
import pydicom
import torch

TARGET = 512
WARMUP = 3
REPEAT = 5

# (DB 변수키, 모듈, 클래스, 가중치파일, max_points, 쓸 점 인덱스)
# two_vbu 는 None — 유효 점(existence>0.5)의 첫/마지막을 쓴다(추론 코드와 동일)
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
    """추론 코드(`load_dicom_image`)와 **동일한** 전처리 + 역변환 정보.

    긴 변을 512 로 맞춰 비율 유지 리사이즈 → 중앙 패딩. 역변환에 쓰려면
    여기서 쓴 pad·scale 을 그대로 알아야 한다.
    """
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

    return padded, {"orig_w": ow, "orig_h": oh, "pad_w": pad_w,
                    "pad_h": pad_h, "scale": max(ow, oh) / TARGET}


def to_original(x512: float, y512: float, geo: dict) -> list[float]:
    """512 패딩 좌표 → DICOM 원본 px. 계측(`Measurement.points`)과 같은 좌표계."""
    return [(x512 - geo["pad_w"]) * geo["scale"],
            (y512 - geo["pad_h"]) * geo["scale"]]


def load_models(models_dir: Path, device: str, on_step=None):
    """8개 모델을 올린다. `on_step(i, n, key)` 로 진행을 알린다."""
    loaded = []
    n = len(MODELS)
    for i, (key, mod_name, cls_name, wfile, maxp, idx) in enumerate(MODELS, 1):
        if on_step:
            on_step(i, n, key)
        wpath = models_dir / wfile
        if not wpath.exists():
            continue
        mod = __import__(mod_name)
        net = getattr(mod, cls_name)(max_points=maxp, use_pretrained=False)
        st = torch.load(wpath, map_location="cpu", weights_only=False)
        net.load_state_dict(st["model_state_dict"] if "model_state_dict" in st else st)
        net.eval().to(device)
        loaded.append((key, net, idx, wfile))
    return loaded


def warmup(loaded, device: str, on_step=None) -> None:
    """커널 컴파일·캐시를 미리 치운다. 이 시간은 측정에 들어가지 않는다."""
    dummy = torch.zeros(1, 1, TARGET, TARGET, device=device)
    with torch.no_grad():
        for r in range(1, WARMUP + 1):
            if on_step:
                on_step(r, WARMUP)
            for _k, net, _i, _w in loaded:
                net(dummy)
    if device == "cuda":
        torch.cuda.synchronize()


def measure_one(net, x, idx, device: str, geo: dict, spacing: float | None):
    """한 항목 — 추론 시간(중앙값)과 원본 좌표·mm."""
    samples = []
    with torch.no_grad():
        for _ in range(REPEAT):
            if device == "cuda":
                torch.cuda.synchronize()
            t = time.perf_counter()
            out = net(x)
            if device == "cuda":
                torch.cuda.synchronize()
            samples.append((time.perf_counter() - t) * 1000)
    ms = statistics.median(samples)

    kp = out["keypoints"][0].detach().cpu().numpy()
    ex = out["existence_probs"][0].detach().cpu().numpy()
    if idx is None:
        valid = [i for i in range(len(ex)) if ex[i] > 0.5]
        pick = (valid[0], valid[-1]) if len(valid) >= 2 else (0, 1)
    else:
        pick = idx
    pts = [to_original(float(kp[i][0]), float(kp[i][1]), geo) for i in pick]
    d = float(np.hypot(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]))
    return {"points": pts, "value_mm": (d * spacing if spacing else None),
            "infer_ms": ms}
