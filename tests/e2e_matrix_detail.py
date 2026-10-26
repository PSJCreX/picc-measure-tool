"""매트릭스 하위 화면 검증.

영상 파일 없이 DB 에 행만 넣어 확인한다 — 서버 렌더는 PNG 바이트를 읽지 않고
URL 문자열만 내보내므로, 좌표·색·토글·뒤로가기를 이것으로 전부 검증할 수 있다.
(브라우저에서 실제로 그려지는지는 사람이 한 번 봐야 한다.)
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

with TestServer() as ts:
    B = ts.base
    a = requests.Session()
    r = a.post(f'{B}/login', data={'username':'admin','password':TEST_ADMIN_PW},
               allow_redirects=True)
    if 'change-password' in r.url:
        a.post(f'{B}/change-password', data={'current_password':TEST_ADMIN_PW,
            'new_password':'Picc-Admin-2026!','confirm_password':'Picc-Admin-2026!'},
            allow_redirects=True)

    # 계측자 2명
    users = []
    for u in ('d1','d2'):
        rr = a.post(f'{B}/admin/users/create',
                    data={'username':u,'full_name':f'의사{u[-1]}','role':'reviewer'},
                    allow_redirects=True)
        assert '임시 비밀번호' in rr.text, rr.text[:200]

    db = ts.data_dir / 'app.db'
    con = sqlite3.connect(db)
    uids = [r[0] for r in con.execute(
        "SELECT id FROM users WHERE role='reviewer' ORDER BY id").fetchall()]
    assert len(uids) == 2, uids

    # 영상 1장 (파일은 없어도 된다 — 서버는 URL 만 넘긴다)
    con.execute("""INSERT INTO images
        (sha256, original_filename, dicom_path, png_path, orig_w, orig_h,
         render_scale, pixel_spacing_mm, spacing_source, status, pool, notes, uploaded_at)
        VALUES ('seed', 'seed.dcm', 'dicom/seed.dcm', 'png/seed.png',
                2000, 2400, 0.4, 0.14, 'PixelSpacing', 'active', 'real', '',
                datetime('now'))""")
    img_id = con.execute("SELECT id FROM images WHERE sha256='seed'").fetchone()[0]

    # 두 계측자가 같은 항목을 다르게 재서 불일치를 만든다 (2RHD: 140 vs 182 mm)
    for i, uid in enumerate(uids):
        span = 1000 + i * 300            # px → ×0.14mm = 140 / 182 mm
        con.execute("""INSERT INTO measurements
            (image_id, user_id, variable_key, points, value_mm, duration_ms,
             attempt, status, created_at, updated_at)
            VALUES (?,?,?,?,?,?,1,'done',datetime('now'),datetime('now'))""",
            (img_id, uid, 'second_ribs_width',
             json.dumps([[300.0,300.0],[300.0+span,300.0]]), span*0.14, 3000))
    # 한 사람만 잰 항목도 하나 (토글에서 1명만 보여야 한다)
    con.execute("""INSERT INTO measurements
        (image_id, user_id, variable_key, points, value_mm, duration_ms,
         attempt, status, created_at, updated_at)
        VALUES (?,?,?,?,?,?,1,'done',datetime('now'),datetime('now'))""",
        (img_id, uids[0], 'right_hvd',
         json.dumps([[500.0,700.0],[1200.0,760.0]]), 98.5, 4200))
    con.commit(); con.close()

    print('=== 하위 화면 ===')
    r = a.get(f'{B}/admin/matrix/image/{img_id}?back=/admin/matrix')
    check(r.status_code == 200, '렌더', f'status={r.status_code} {r.text[:200]}')
    if r.status_code != 200:
        raise SystemExit(1)
    t = r.text

    check('← 매트릭스로' in t, '좌상단 뒤로 가기')
    check('href="/admin/matrix"' in t, '뒤로 가기 대상 = 매트릭스')
    check('detCanvas' in t, '영상 캔버스 (표 자리에)')
    check('matrix_detail.js' in t, '뷰어 스크립트')
    check('data-rater="all"' in t, '계측자 "전체 겹쳐 보기" 토글')
    check(t.count('data-rater=') >= 3, f'계측자별 토글 ({t.count("data-rater=")-1}명)')
    check('data-var="all"' in t, '항목 "전부" 토글')
    check(t.count('data-var=') >= 9, f'항목별 토글 ({t.count("data-var=")-1}개)')

    m = re.search(r'<script id="detailData"[^>]*>(.*?)</script>', t, re.S)
    check(m is not None, 'detailData 전달')
    d = json.loads(m.group(1))
    check(d['renderScale'] == 0.4, f"renderScale 전달 ({d['renderScale']})")
    check(len(d['raters']) == 2, f"계측자 2명 ({len(d['raters'])})")
    colors = [x['color'] for x in d['raters']]
    check(len(set(colors)) == 2, f'색이 서로 다름 {colors}')

    r1 = [x for x in d['raters'] if x['n'] == 2]
    r2 = [x for x in d['raters'] if x['n'] == 1]
    check(len(r1) == 1 and len(r2) == 1,
          f"계측 수가 사람별로 다르게 전달 ({[x['n'] for x in d['raters']]})")
    # lines 는 항목 순서(ALL_VARIABLE_KEYS)로 정렬되므로 키로 찾는다
    by_key = {x['key']: x for x in r1[0]['lines']}
    check(set(by_key) == {'right_hvd', 'second_ribs_width'},
          f'두 항목이 전달됨 {sorted(by_key)}')
    it = by_key['second_ribs_width']
    check(it['points'] == [[300.0, 300.0], [1300.0, 300.0]],
          '좌표가 DICOM 원본 px 그대로 전달', str(it['points']))
    check(abs(it['value_mm'] - 140.0) < 0.05, f"mm 값 ({it['value_mm']})")
    hvd = by_key['right_hvd']
    check(hvd['points'] == [[500.0, 700.0], [1200.0, 760.0]],
          '다른 항목 좌표도 그대로', str(hvd['points']))

    check('⚠' in t, '불일치 항목에 경고 표시')
    check('140.0' in t and '182.0' in t, '두 계측값이 표에 나옴')
    check('42.0' in t, '차이(182−140=42mm) 표시')

    print('=== 뒤로 가기 ===')
    r2x = a.get(f'{B}/admin/matrix/image/{img_id}?back=/admin/disagreements')
    check('href="/admin/disagreements"' in r2x.text, '불일치에서 오면 그쪽으로 복귀')
    r3 = a.get(f'{B}/admin/matrix/image/{img_id}?back=https://evil.example/x')
    check('evil.example' not in r3.text and 'href="/admin/matrix"' in r3.text,
          '외부 주소는 기본값으로 대체 (열린 리다이렉트 차단)')

    print('=== 매트릭스 연결 ===')
    r4 = a.get(f'{B}/admin/matrix')
    check(f'/admin/matrix/image/{img_id}' in r4.text, '행 클릭이 하위 화면을 가리킴')
    check('focus-panel' not in r4.text or '?focus=' not in r4.text,
          '좁은 우측 패널 방식이 더 이상 행 클릭에 쓰이지 않음')

    r5 = a.get(f'{B}/admin/matrix/image/999999')
    check(r5.status_code == 404, '없는 영상은 404')

print(f'\n통과 {ok} / 실패 {fail}')
sys.exit(1 if fail else 0)
