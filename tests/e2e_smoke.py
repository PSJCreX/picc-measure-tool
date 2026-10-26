"""엔드투엔드 스모크 테스트.

서버를 띄운 뒤 실행한다:

    .venv\\Scripts\\python.exe -m uvicorn app.main:app --port 1531
    .venv\\Scripts\\python.exe tests\\e2e_smoke.py

계측 단위는 (영상 × 변수) 문제 하나다. 모든 영상이 8개 항목 전부의 대상이다.

기존 데이터가 남아 있는 DB에서도 돌아가지만(계정·영상 재사용),
**정확한 검증은 초기화 직후**에 하는 것이 좋다:

    관리자 화면 › 초기화 › 전체 초기화
    또는 서버를 멈추고 data/app.db · data/dicom · data/png 삭제
"""
from __future__ import annotations

import io
import re
import sys
import zipfile
from pathlib import Path

import requests

# 콘솔 기본 인코딩(cp949)에서 em-dash 같은 문자가 깨지지 않게
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[2]          # AI-PICC project/
PROJECT = Path(__file__).resolve().parents[1]       # web_labeling/


def _port_from_env(default: int = 1531) -> int:
    """.env의 WEBAPP_PORT를 따라간다 — 포트를 바꿔도 테스트가 따라오게."""
    env = PROJECT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("WEBAPP_PORT"):
                _, _, v = line.partition("=")
                if v.strip().isdigit():
                    return int(v.strip())
    return default


# BASE 는 main() 에서 정한다. 기본은 임시 DB로 띄우는 전용 서버.
BASE = ""
ADMIN_USER, ADMIN_TEMP, ADMIN_PW = "admin", "picc-admin-2026", "adminPass!2026"

VARS = [
    "right_clavicle", "left_clavicle", "right_hvd", "left_hvd",
    "right_tcd", "left_tcd", "second_ribs_width", "two_vbu",
]

# 항목별 기준 길이(원본 px). 첫 문제는 무작위로 나오므로 target 영상에 걸릴 수
# 있다 — 그때 다른 값을 넣으면 뒤에서 같은 항목을 다시 답할 수 없어(409)
# 계측자 간 값이 어긋난다. 그래서 어디서든 같은 표를 쓴다.
BASE_SPAN = {
    "right_clavicle": 1000.0, "left_clavicle": 1010.0,
    "right_hvd": 500.0, "left_hvd": 505.0,
    "right_tcd": 300.0, "left_tcd": 305.0,
    "second_ribs_width": 1400.0, "two_vbu": 600.0,
}
MM_PER_PX = 0.14   # 이 테스트가 쓰는 Rt 영상들의 픽셀 스페이싱

FAILS: list[str] = []
STEP = [0]


def check(cond: bool, label: str, detail: str = "") -> bool:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" +
          (f"  — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)
    return cond


def _check_client_js_syntax() -> None:
    """static/*.js 가 파싱되는지 확인. node 가 없으면 건너뛴다(경고만)."""
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    static = Path(__file__).resolve().parents[1] / "static"
    scripts = sorted(static.glob("*.js"))
    if not node:
        print("       (node 없음 — 클라이언트 JS 문법 검사 건너뜀)")
        return
    for js in scripts:
        p = subprocess.run([node, "--check", str(js)],
                           capture_output=True, text=True)
        check(p.returncode == 0, f"{js.name} 문법",
              (p.stderr or "").strip().splitlines()[-1] if p.stderr else "")


def step(title: str) -> None:
    STEP[0] += 1
    print(f"\n=== {STEP[0]}. {title} ===")


def login(s: requests.Session, user: str, pw: str):
    return s.post(f"{BASE}/login", data={"username": user, "password": pw},
                  allow_redirects=True)


def change_pw(s: requests.Session, old: str, new: str):
    return s.post(f"{BASE}/change-password",
                  data={"current_password": old, "new_password": new,
                        "confirm_password": new}, allow_redirects=True)


def answer(session, image_id, key, span_px, duration_ms=4200):
    """한 문제에 답한다. 시작점 (300,300) 에서 span_px 만큼 수평으로."""
    return session.post(f"{BASE}/measure/answer", json={
        "image_id": image_id, "variable_key": key,
        "points": [[300.0, 300.0], [300.0 + span_px, 300.0]],
        "duration_ms": duration_ms,
    })


def _answers_for(session, image_id: int) -> list[int]:
    """이 계측자가 해당 영상에 남긴 유효 계측 id 목록."""
    r = session.get(f"{BASE}/measure/next")
    out = []
    for a in r.json().get("answers", []) or []:
        if a.get("imageId") == image_id:
            out.append(a["id"])
    return out


def run_suite() -> int:
    # ---------------------------------------------------------------- 1
    step("관리자 로그인 + 비밀번호 변경")
    admin = requests.Session()
    # 신규 DB면 .env의 임시 비밀번호로, 이미 한 번 돌린 DB면 바뀐 비밀번호로 붙는다.
    # 테스트를 두 번 이상 돌려도 여기서 막히지 않게.
    r = login(admin, ADMIN_USER, ADMIN_TEMP)
    fresh = "change-password" in r.url
    if fresh:
        check(r.status_code == 200, "관리자 로그인(임시 비밀번호)", f"status={r.status_code}")
        check(True, "임시 비밀번호 → 변경 화면 강제")
        r = change_pw(admin, ADMIN_TEMP, ADMIN_PW)
        check(r.status_code == 200 and "/admin" in r.url,
              "비밀번호 변경 후 관리자 홈", r.url)
    else:
        admin = requests.Session()
        r = login(admin, ADMIN_USER, ADMIN_PW)
        check(r.status_code == 200 and "/admin" in r.url,
              "관리자 로그인(기존 DB, 변경된 비밀번호)", r.url)
        print("       (기존 데이터가 있는 DB입니다 — 비밀번호 강제 변경 검증은 건너뜁니다)")

    # ---------------------------------------------------------------- 2
    step("계측자 2명 생성")
    docs: dict[str, requests.Session] = {}
    for uname, name in (("doc1", "김전문"), ("doc2", "이전문")):
        final_pw = uname + "Pass!2026"

        # 이미 있는 계정이면 그대로 재사용한다(테스트 반복 실행 대응)
        probe = requests.Session()
        if "/measure" in login(probe, uname, final_pw).url:
            check(True, f"{uname} 기존 계정 재사용")
            docs[uname] = probe
            continue

        r = admin.post(f"{BASE}/admin/users/create",
                       data={"username": uname, "full_name": name,
                             "role": "reviewer"}, allow_redirects=True)
        check("?" not in r.url, f"{uname} 임시비밀번호가 URL에 노출되지 않음", r.url[:120])
        m = re.search(r"임시 비밀번호:\s*([A-Za-z0-9]+)", r.text)
        if not check(bool(m), f"{uname} 생성 + 임시비밀번호 회수", r.text[:160]):
            continue

        s = requests.Session()
        login(s, uname, m.group(1))
        r = change_pw(s, m.group(1), final_pw)
        check("/measure" in r.url, f"{uname} 비밀번호 변경 → 계측 화면", r.url)
        docs[uname] = s
    if len(docs) < 2:
        print("\n계측자 생성 실패로 중단")
        return 1

    # ---------------------------------------------------------------- 3
    step("허용범위 5mm 설정")
    r = admin.post(f"{BASE}/admin/settings",
                   data={"tolerance_mm": "5.0", "render_max_px": "2048",
                         "idle_lock_ms": "60000", "idle_phase1_ms": "10000"},
                   allow_redirects=True)
    check(r.status_code == 200, "설정 저장", f"status={r.status_code}")

    # ---------------------------------------------------------------- 4
    step("DICOM 업로드 (TASK 그룹 없이)")
    src = sorted((ROOT / "추가 data (20260112 이후 시술분)" / "Rt").glob("*.dcm"))[:3]
    check(len(src) == 3, "테스트용 DICOM 확보", f"found={len(src)}")

    files = [("files", (p.name, p.read_bytes(), "application/dicom")) for p in src]
    # pool 은 필수다 — 빠지면 서버가 거부한다(연습/실제 오배치 방지).
    r = admin.post(f"{BASE}/admin/upload", files=files, data={"pool": "real"})
    ok = r.status_code == 200 and r.json().get("ok")
    check(ok, "업로드 요청", f"status={r.status_code} body={r.text[:200]}")
    results = r.json().get("results", []) if ok else []
    # 이미 올린 DB에서 다시 돌리면 duplicate 로 돌아온다 — 둘 다 등재된 상태다.
    image_ids = [x["image_id"] for x in results
                 if x["status"] in ("ok", "duplicate") and x["image_id"]]
    n_new = sum(1 for x in results if x["status"] == "ok")
    check(len(image_ids) == 3, "3장 등재(신규 또는 기존)",
          f"new={n_new} total={len(image_ids)} {str(results)[:160]}")
    spacings = {x.get("spacing_source") for x in results if x["status"] == "ok"}
    check(all(s in ("PixelSpacing", "ImagerPixelSpacing") for s in spacings if s),
          "픽셀 스페이싱 해석됨", str(spacings))

    r2 = admin.post(f"{BASE}/admin/upload",
                    files=[("files", (src[0].name, src[0].read_bytes(),
                                      "application/dicom"))],
                    data={"pool": "real"})
    check(r2.json()["results"][0]["status"] == "duplicate", "동일 파일 재업로드 → 중복")
    if not image_ids:
        return 1

    # ---------------------------------------------------------------- 5
    step("문제 출제 — 완전 무작위 (영상 × 항목)")
    r = docs["doc1"].get(f"{BASE}/measure/next")
    j = r.json()
    check(r.status_code == 200 and j.get("ok") and j.get("question"), "문제 발급")
    q = j["question"]
    check(q["variableKey"] in VARS, "8개 항목 중 하나", q.get("variableKey"))
    check("pngUrl" in q and "instruction" in q and "guide" in q,
          "문제에 안내·그림·영상 URL 포함")
    check(j["progress"]["total"] == 3 * 8, "분모 = 영상 3장 × 8항목",
          str(j["progress"]))

    # 사진은 문제와 분리되어 따로 받아 온다(미리 받아 두기 위해)
    r = docs["doc1"].get(f"{BASE}{q['pngUrl']}")
    check(r.status_code == 200 and r.content[:4] == b"\x89PNG", "영상 PNG 전송")

    # ---------------------------------------------------------------- 6
    step("답안 저장 + 중복 방지")
    span = BASE_SPAN[q["variableKey"]]
    expected_mm = round(span * MM_PER_PX, 1)
    before = docs["doc1"].get(f"{BASE}/measure/next").json()["progress"]["done"]
    r = answer(docs["doc1"], q["imageId"], q["variableKey"], span)
    if r.status_code == 409:
        # 기존 DB에서 재실행 — 이미 답한 문제라면 초기화 후 다시 돌려야 정확하다
        print("       (이미 답한 문제입니다 — 정확한 검증은 초기화 후 재실행하세요)")
    j = r.json()
    check(r.status_code == 200 and j.get("ok"), "답안 저장", r.text[:160])
    check(j.get("value_mm") == expected_mm,
          f"mm 환산 ({span:.0f}px × {MM_PER_PX})",
          f"got={j.get('value_mm')} expected={expected_mm}")
    check(j["progress"]["done"] == before + 1, "진행률 1 증가",
          f"{before} → {j['progress']['done']}")
    n_answers = len(j.get("answers", []))
    check(n_answers == before + 1 or n_answers >= 1, "완료 목록에 누적",
          f"n={n_answers}")
    first_mid = j["measurement_id"]

    r = answer(docs["doc1"], q["imageId"], q["variableKey"], span)
    check(r.status_code == 409, "같은 문제 중복 제출 거부", f"status={r.status_code}")

    seen = set()
    for _ in range(12):
        jj = docs["doc1"].get(f"{BASE}/measure/next").json()
        if jj.get("question"):
            seen.add((jj["question"]["imageId"], jj["question"]["variableKey"]))
    check((q["imageId"], q["variableKey"]) not in seen,
          "답한 문제는 pool에서 빠짐")

    # ---------------------------------------------------------------- 7
    step("재측정 요청 → 대기 pool 회귀")
    r = docs["doc1"].post(f"{BASE}/measure/redo",
                          json={"measurement_id": first_mid})
    j = r.json()
    check(r.status_code == 200 and j.get("ok"), "재측정 요청", r.text[:160])
    check(j["progress"]["done"] == before, "진행률에서 빠짐",
          f"기대 {before}, 실제 {j['progress']['done']}")
    check(all(a["id"] != first_mid for a in j.get("answers", [])),
          "완료 목록에서 사라짐")

    back = False
    for _ in range(80):
        jj = docs["doc1"].get(f"{BASE}/measure/next").json()
        if jj.get("question") and \
           (jj["question"]["imageId"], jj["question"]["variableKey"]) == \
           (q["imageId"], q["variableKey"]):
            back = True
            break
    check(back, "대기 pool로 되돌아와 다시 출제됨")

    r = answer(docs["doc1"], q["imageId"], q["variableKey"], span)
    check(r.json().get("ok"), "재측정 후 다시 답안 저장")

    r = docs["doc1"].post(f"{BASE}/measure/redo", json={"measurement_id": first_mid})
    check(r.status_code == 409, "이미 재측정 요청된 건 재요청 거부",
          f"status={r.status_code}")

    # ---------------------------------------------------------------- 8
    step("두 계측자가 같은 영상의 8개 항목 전부 계측")
    target = image_ids[0]

    # 이 영상에 남아 있는 앞선 계측을 비운다 — 다른 값으로 답해 두었으면
    # 계측자 간 값이 어긋나 일치 판정이 흔들린다(테스트 반복 실행 대응).
    for sess in docs.values():
        for mid in _answers_for(sess, target):
            sess.post(f"{BASE}/measure/redo", json={"measurement_id": mid})

    for key, sp in BASE_SPAN.items():
        rr = answer(docs["doc1"], target, key, sp)
        if rr.status_code == 409:
            continue  # 앞 단계에서 이미 같은 값으로 답한 항목
        check(rr.json().get("ok"), f"doc1 {key}", rr.text[:120])

    span2 = dict(BASE_SPAN)
    span2["right_tcd"] = 300.0 + 200.0        # 200px × 0.14 = 28mm → 불일치
    span2["left_clavicle"] = 1010.0 + 10.0    # 1.4mm → 허용범위 내
    for key, sp in span2.items():
        rr = answer(docs["doc2"], target, key, sp)
        check(rr.status_code == 200 and rr.json().get("ok"), f"doc2 {key}",
              rr.text[:120])

    # ---------------------------------------------------------------- 9
    step("매트릭스 · 불일치")
    marker = f"?focus={target}'"
    r = admin.get(f"{BASE}/admin/matrix?focus={target}")
    check(r.status_code == 200, "매트릭스 페이지", f"status={r.status_code}")
    check("불일치" in r.text, "불일치 표기 존재")
    check("Rt. TCD" in r.text and "2nd Ribs Width" in r.text, "8개 항목 모두 표시")

    r = admin.get(f"{BASE}/admin/disagreements")
    check(r.status_code == 200 and marker in r.text, "불일치 목록에 해당 영상 포함")

    # ---------------------------------------------------------------- 10
    step("허용범위 변경 시 재분류")
    admin.post(f"{BASE}/admin/settings",
               data={"tolerance_mm": "50.0", "render_max_px": "2048",
                     "idle_lock_ms": "60000", "idle_phase1_ms": "10000"})
    r = admin.get(f"{BASE}/admin/disagreements")
    check(marker not in r.text, "허용범위 50mm → 불일치 해소")
    admin.post(f"{BASE}/admin/settings",
               data={"tolerance_mm": "5.0", "render_max_px": "2048",
                     "idle_lock_ms": "60000", "idle_phase1_ms": "10000"})
    r = admin.get(f"{BASE}/admin/disagreements")
    check(marker in r.text, "허용범위 5mm 복원 → 불일치 재등장")

    # ---------------------------------------------------------------- 11
    step("확정값(GT) 층이 제거되었는지")
    # 이 연구는 계측을 하나로 합의시키지 않는다. 중재 라우트는 존재하면 안 된다.
    for path in ("/admin/gt/save", "/admin/gt/clear"):
        r = admin.post(f"{BASE}{path}",
                       data={"image_id": target, "variable_key": "right_tcd",
                             "value_mm": "44.44"}, allow_redirects=False)
        check(r.status_code == 404, f"{path} 없음 (404)")

    r = admin.get(f"{BASE}/admin/matrix?focus={target}")
    check(r.status_code == 200, "매트릭스 정상 렌더")
    check("관리자 중재" not in r.text and "확정값" not in r.text,
          "매트릭스에 확정/중재 UI 없음")

    r = admin.get(f"{BASE}/admin/export")
    check(r.status_code == 200 and "2인 이상이 잰" in r.text,
          "내보내기 화면이 '2인 이상 계측' 진척도를 보여줌")

    # ---------------------------------------------------------------- 12
    step("내보내기")
    for path, label, minlen in (
        ("/admin/export/wide.xlsx", "wide XLSX", 5000),
        ("/admin/export/long.csv", "long CSV", 200),
        ("/admin/export/disagreements.xlsx", "불일치 XLSX", 4000),
        ("/admin/export/bundle.zip", "데이터 번들 ZIP", 5000),
    ):
        r = admin.get(f"{BASE}{path}")
        check(r.status_code == 200 and len(r.content) >= minlen, label,
              f"status={r.status_code} len={len(r.content)}")

    text = admin.get(f"{BASE}/admin/export/long.csv").content.decode("utf-8-sig")
    check("duration_ms" in text and "spread_mm" in text and "status" in text,
          "long CSV에 소요시간·차이·상태 열 포함")
    check("redo" in text, "long CSV에 버려진 시도도 포함")
    check(text.count("right_tcd") >= 2, "long CSV에 두 계측자 값 포함")
    check("gt_value_mm" not in text and "gt_source" not in text,
          "long CSV에 GT 열 없음")

    import zipfile as _zf
    blob = admin.get(f"{BASE}/admin/export/bundle.zip").content
    names = _zf.ZipFile(io.BytesIO(blob)).namelist()
    anns = [n for n in names if n.startswith("annotations/")]
    check(len(anns) > 0, "번들에 어노테이션 JSON이 들어 있음",
          f"annotations/ 항목 {len(anns)}개")
    raters = {n.split("/")[1] for n in anns}
    check(len(raters) >= 2, "어노테이션이 계측자별로 분리됨",
          f"계측자 폴더: {sorted(raters)}")
    one = _zf.ZipFile(io.BytesIO(blob)).read(anns[0]).decode("utf-8")
    check('"rater"' in one and '"duration_ms"' in one,
          "어노테이션 JSON에 계측자·소요시간 포함")

    with _zf.ZipFile(io.BytesIO(blob)) as zf:
        check(zf.testzip() is None, "번들 ZIP 무결성(전 멤버 헤더 정상)")
        n_read = sum(1 for n in zf.namelist() if zf.read(n) is not None)
        check(n_read == len(zf.namelist()),
              "번들 ZIP 전 멤버가 실제로 압축 해제됨",
              f"{n_read}/{len(zf.namelist())}")
    check(any("/right_clavicle/" in n for n in names),
          "번들에 학습용 어노테이션 JSON 포함", str(names[:8]))

    # ---------------------------------------------------------------- 13
    step("역할 분리 · 접근 제어")
    r = docs["doc1"].get(f"{BASE}/admin/matrix", allow_redirects=False)
    check(r.status_code in (302, 303, 403), "계측자는 관리자 화면 접근 불가",
          f"status={r.status_code}")
    r = admin.get(f"{BASE}/measure", allow_redirects=False)
    check(r.status_code in (302, 303, 403), "관리자는 계측 화면 접근 불가",
          f"status={r.status_code}")
    r = requests.Session().get(f"{BASE}/measure", allow_redirects=False)
    check(r.status_code in (302, 303, 401), "비로그인 차단", f"status={r.status_code}")

    r = docs["doc2"].post(f"{BASE}/measure/redo", json={"measurement_id": first_mid})
    check(r.status_code == 404, "남의 계측은 재측정 요청 불가",
          f"status={r.status_code}")

    # ---------------------------------------------------------------- 14
    step("전 화면 렌더 확인")
    for path in ("/admin", "/admin/upload", "/admin/users", "/admin/matrix",
                 f"/admin/matrix?focus={target}", "/admin/disagreements",
                 "/admin/settings", "/admin/export", "/admin/reset"):
        r = admin.get(f"{BASE}{path}")
        check(r.status_code == 200 and "Internal Server Error" not in r.text,
              f"GET {path}", f"status={r.status_code}")
    for path in ("/measure", "/measure/paused", "/history", "/change-password"):
        r = docs["doc1"].get(f"{BASE}{path}")
        check(r.status_code == 200 and "Internal Server Error" not in r.text,
              f"GET {path} (계측자)", f"status={r.status_code}")

    r = docs["doc1"].get(f"{BASE}/measure")
    check("다음 계측 항목" in r.text, "계측 화면에 문제 카드 표시")
    check("영상 준비 중" in r.text, "사진은 준비 완료 전까지 감춰짐")

    # 브리핑 화면은 서버가 뼈대만 내려보내고 measure.js 가 채운다. 그래서
    # measure.js 가 문법 오류로 죽어도 위의 HTML 검사는 전부 통과한다 —
    # 실제로 한 번 그렇게 새어 나갔다(정규식 안에 줄바꿈이 들어가 파싱 실패).
    # node 가 있으면 문법만이라도 확인한다.
    _check_client_js_syntax()

    for iid in image_ids:
        for key in VARS:
            answer(docs["doc2"], iid, key, 700.0)
    r = docs["doc2"].get(f"{BASE}/measure")
    check("계측할 영상이 없습니다" in r.text, "전량 완료 시 완료 화면")

    # ---------------------------------------------------------------- 15
    step("초기화 안전장치")
    r = admin.get(f"{BASE}/admin/reset")
    check(r.status_code == 200 and "계측 기록만 초기화" in r.text, "초기화 화면")

    r = admin.post(f"{BASE}/admin/reset",
                   data={"scope": "measurements", "password": "wrong-password",
                         "confirm_phrase": "계측 기록 삭제"}, allow_redirects=True)
    check("비밀번호가 올바르지 않습니다" in r.text, "틀린 비밀번호 거부")
    check(len(admin.get(f"{BASE}/admin/export/long.csv")
              .content.decode("utf-8-sig").strip().split("\n")) > 1,
          "  → 데이터 보존됨")

    r = admin.post(f"{BASE}/admin/reset",
                   data={"scope": "measurements", "password": ADMIN_PW,
                         "confirm_phrase": "아무거나"}, allow_redirects=True)
    check("확인 문구가 다릅니다" in r.text, "틀린 확인 문구 거부")

    r = docs["doc1"].post(f"{BASE}/admin/reset",
                          data={"scope": "all", "password": "doc1Pass!2026",
                                "confirm_phrase": "전체 삭제"},
                          allow_redirects=False)
    check(r.status_code in (302, 303, 403), "계측자의 초기화 시도 차단",
          f"status={r.status_code}")

    r = admin.post(f"{BASE}/admin/reset",
                   data={"scope": "measurements", "password": ADMIN_PW,
                         "confirm_phrase": "계측 기록 삭제"}, allow_redirects=True)
    check("완료" in r.text, "올바른 입력으로 초기화 실행")
    check(len(admin.get(f"{BASE}/admin/export/long.csv")
              .content.decode("utf-8-sig").strip().split("\n")) <= 1,
          "  → 계측 기록이 비워짐")
    check("doc1" in admin.get(f"{BASE}/admin/users").text, "  → 계정은 남아 있음")

    # ---------------------------------------------------------------- 결과
    print("\n" + "=" * 60)
    if FAILS:
        print(f"실패 {len(FAILS)}건:")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("전체 통과")
    return 0


def main() -> int:
    """기본은 임시 DB 위에 전용 서버를 띄워 돌린다.

    이 테스트는 마지막에 `/admin/reset` 을 **실제로** 호출해 계측 기록을 지운다.
    운영 서버에 물리면 사람이 만든 데이터가 사라진다 — 실제로 한 번 그렇게
    날려 먹었다. 그래서 격리가 기본값이고, 운영 서버로 돌리려면
    `--live` 를 명시해야 한다.
    """
    global BASE
    from harness import TestServer, guard_live_target

    if "--live" in sys.argv:
        BASE = f"http://127.0.0.1:{_port_from_env()}"
        guard_live_target(BASE, allow_live=True)
        return run_suite()

    print("임시 DB로 전용 테스트 서버를 띄웁니다 (운영 DB는 건드리지 않습니다).")
    with TestServer() as srv:
        BASE = srv.base
        print(f"  → {BASE}  (DATA_DIR={srv.data_dir})")
        return run_suite()


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
