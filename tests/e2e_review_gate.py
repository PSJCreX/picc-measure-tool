"""계측자 비교 화면 게이트 검증.

지키려는 것 — **눈가림.** 계측자가 서로의 계측값과 AI 예측을 보면 이후 계측이
오염된다. 열림 조건은 둘 다 만족해야 한다:

    관리자가 '공개' 를 켰다  AND  그 계측자의 남은 작업이 0

'남은 작업 0' 만으로 열면 안 되는 이유가 실제로 있었다 — test set 519장 중
159장만 올라간 상태에서 두 계측자의 남은 작업이 0이었다. 그때 열면 남은 350장
계측이 전부 오염된다.

    .venv/bin/python tests/e2e_review_gate.py
"""
import json, re, sqlite3, sys
sys.path.insert(0, 'tests')
from harness import TestServer, TEST_ADMIN_PW
import requests

ok = fail = 0
def check(c, label, extra=''):
    global ok, fail
    if c: ok += 1; print(f'  [PASS] {label}')
    else: fail += 1; print(f'  [FAIL] {label}   {extra}')

# 계측자에게 열리는 것은 매트릭스와 영상 상세뿐이다.
# 불일치 화면은 관리자 전용으로 되돌렸다(2026-10-07 요청).
VIEW_PATHS = ('/admin/matrix',)
ADMIN_ONLY = ('/admin/disagreements', '/admin/upload', '/admin/users',
              '/admin/reset', '/admin/export', '/admin/settings')

with TestServer() as ts:
    B = ts.base
    a = requests.Session()
    r = a.post(f'{B}/login', data={'username':'admin','password':TEST_ADMIN_PW},
               allow_redirects=True)
    if 'change-password' in r.url:
        a.post(f'{B}/change-password', data={'current_password':TEST_ADMIN_PW,
            'new_password':'Picc-Admin-2026!','confirm_password':'Picc-Admin-2026!'},
            allow_redirects=True)

    # 계측자 1명
    r = a.post(f'{B}/admin/users/create',
               data={'username':'d1','full_name':'의사1','role':'reviewer'},
               allow_redirects=True)
    tmp = re.search(r"임시 비밀번호:\s*([A-Za-z0-9]+)", r.text).group(1)
    doc = requests.Session()
    doc.post(f'{B}/login', data={'username':'d1','password':tmp}, allow_redirects=True)
    doc.post(f'{B}/change-password', data={'current_password':tmp,
        'new_password':'D1-Pass-2026!','confirm_password':'D1-Pass-2026!'},
        allow_redirects=True)

    # 영상 1장 + 계측 (파일 없이 DB 로 — 서버 렌더는 바이트를 읽지 않는다)
    db = sqlite3.connect(ts.data_dir / 'app.db')
    uid = db.execute("SELECT id FROM users WHERE username='d1'").fetchone()[0]
    db.execute("""INSERT INTO images (sha256, original_filename, dicom_path, png_path,
        orig_w, orig_h, render_scale, pixel_spacing_mm, spacing_source, status, pool,
        notes, uploaded_at) VALUES ('g','g.dcm','dicom/g.dcm','png/g.png',
        2000,2400,0.4,0.14,'PixelSpacing','active','real','',datetime('now'))""")
    img = db.execute("SELECT id FROM images WHERE sha256='g'").fetchone()[0]
    db.commit()

    print('=== 1. 기본: 공개 꺼짐 → 계측자 차단 ===')
    for p in VIEW_PATHS:
        rr = doc.get(f'{B}{p}', allow_redirects=False)
        check(rr.status_code == 403, f'{p} 차단 ({rr.status_code})')
    rr = doc.get(f'{B}/admin/matrix/image/{img}', allow_redirects=False)
    check(rr.status_code == 403, f'영상 상세 차단 ({rr.status_code})')
    rr = doc.get(f'{B}/admin/image/{img}/predict', allow_redirects=False)
    check(rr.status_code == 403, f'AI 추론 차단 ({rr.status_code})')
    rr = a.get(f'{B}/admin/matrix')
    check(rr.status_code == 200, '관리자는 항상 열림')

    print('=== 2. 공개 켬 + 남은 작업 있음 → 여전히 차단 ===')
    a.post(f'{B}/admin/settings', data={'tolerance_mm':'5.0','render_max_px':'2048',
        'idle_lock_ms':'60000','idle_phase1_ms':'10000',
        'reviewer_review_access':'1'}, allow_redirects=True)
    rr = doc.get(f'{B}/admin/matrix', allow_redirects=False)
    check(rr.status_code == 403, f'남은 작업 8건 → 차단 ({rr.status_code})')
    check('남은 계측' in rr.text, '이유를 알려줌', rr.text[:120])

    print('=== 2b. 불일치 화면은 계측자에게 끝까지 닫혀 있다 ===')
    rr = doc.get(f'{B}/admin/disagreements', allow_redirects=False)
    check(rr.status_code == 403, f'불일치 차단 ({rr.status_code})')

    print('=== 3. 계측 완료 → 열림 ===')
    for i, vk in enumerate(['right_clavicle','left_clavicle','right_hvd','left_hvd',
                            'right_tcd','left_tcd','second_ribs_width','two_vbu']):
        db.execute("""INSERT INTO measurements (image_id, user_id, variable_key, points,
            value_mm, duration_ms, attempt, status, created_at, updated_at)
            VALUES (?,?,?,?,?,?,1,'done',datetime('now'),datetime('now'))""",
            (img, uid, vk, json.dumps([[300.0,300.0],[1300.0,300.0]]), 140.0, 5000+i))
    db.commit()
    rr = doc.get(f'{B}/admin/matrix')
    check(rr.status_code == 200, f'비교 화면 열림 ({rr.status_code})')
    rr = doc.get(f'{B}/admin/matrix/image/{img}')
    check(rr.status_code == 200, f'영상 상세 열림 ({rr.status_code})')
    check('계측 비교' in doc.get(f'{B}/history').text, '네비게이션에 링크 등장')

    print('=== 4. 로그인 시 비교 화면으로 이동 ===')
    d2 = requests.Session()
    rr = d2.post(f'{B}/login', data={'username':'d1','password':'D1-Pass-2026!'},
                 allow_redirects=True)
    check('/admin/matrix' in rr.url, f'계측 완료자 → 매트릭스 ({rr.url})')

    print('=== 5. 새 영상이 올라오면 다시 닫힌다 (핵심) ===')
    db.execute("""INSERT INTO images (sha256, original_filename, dicom_path, png_path,
        orig_w, orig_h, render_scale, pixel_spacing_mm, spacing_source, status, pool,
        notes, uploaded_at) VALUES ('g2','g2.dcm','dicom/g2.dcm','png/g2.png',
        2000,2400,0.4,0.14,'PixelSpacing','active','real','',datetime('now'))""")
    db.commit()
    rr = doc.get(f'{B}/admin/matrix', allow_redirects=False)
    check(rr.status_code == 403, f'새 작업 생김 → 자동 차단 ({rr.status_code})')
    d3 = requests.Session()
    rr = d3.post(f'{B}/login', data={'username':'d1','password':'D1-Pass-2026!'},
                 allow_redirects=True)
    check('/measure' in rr.url, f'로그인도 계측 화면으로 되돌아감 ({rr.url})')

    print('=== 6. 공개를 끄면 닫힌다 ===')
    db.execute("DELETE FROM images WHERE sha256='g2'"); db.commit()
    check(doc.get(f'{B}/admin/matrix').status_code == 200, '다시 열림(작업 0)')
    a.post(f'{B}/admin/settings', data={'tolerance_mm':'5.0','render_max_px':'2048',
        'idle_lock_ms':'60000','idle_phase1_ms':'10000'}, allow_redirects=True)
    rr = doc.get(f'{B}/admin/matrix', allow_redirects=False)
    check(rr.status_code == 403, f'공개 끔 → 차단 ({rr.status_code})')
    check('공개되지 않았습니다' in rr.text, '이유를 알려줌')

    print('=== 7. 파괴적 라우트는 계측자에게 계속 막혀 있다 ===')
    a.post(f'{B}/admin/settings', data={'tolerance_mm':'5.0','render_max_px':'2048',
        'idle_lock_ms':'60000','idle_phase1_ms':'10000',
        'reviewer_review_access':'1'}, allow_redirects=True)
    check(doc.get(f'{B}/admin/matrix').status_code == 200, '비교 화면은 열림')
    for p in ADMIN_ONLY:
        rr = doc.get(f'{B}{p}', allow_redirects=False)
        check(rr.status_code == 403, f'{p} 차단 ({rr.status_code})')

    print('=== 8. 계측자 화면에 관리자 메뉴·링크가 노출되지 않는다 ===')
    page = doc.get(f'{B}/admin/matrix').text
    for frag, label in (('/admin/disagreements', '불일치'),
                        ('/admin/export', '내보내기'),
                        ('/admin/users', '사용자')):
        check(frag not in page, f'매트릭스에 {label} 링크 없음')
    det = doc.get(f'{B}/admin/matrix/image/{img}').text
    check('/admin/export' not in det, '상세에 DICOM 받기(내보내기) 없음')
    for frag, label in (('/admin/upload', '업로드'), ('/admin/settings', '설정'),
                        ('/admin/reset', '초기화')):
        check(frag not in page and frag not in det, f'{label} 링크 없음')

    print('=== 9. 관리자 화면에는 그 메뉴가 그대로 보인다 ===')
    apage = a.get(f'{B}/admin/matrix').text
    check('/admin/disagreements' in apage, '관리자에게 불일치 링크 보임')
    check('/admin/export' in apage, '관리자에게 내보내기 링크 보임')
    db.close()

print(f'\n통과 {ok} / 실패 {fail}')
sys.exit(1 if fail else 0)
