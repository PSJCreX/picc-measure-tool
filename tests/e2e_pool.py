r"""연습 pool / 실제 계측 pool 분리 검증.

**왜 따로 두는가** — 연습 기록이 연구 산출물에 한 줄이라도 섞이면 논문 자료가
오염된다. 그런데 그 경로는 조용하다: 화면은 멀쩡히 돌아가고, CSV를 열어 보기
전까지 아무도 모른다. 그래서 '섞이지 않는다'를 단정적으로 확인하는 테스트를
따로 둔다.

`tests/harness.py` 를 쓰므로 **임시 DB로 서버를 따로 띄운다** — 운영 서버(1531)는
건드리지 않는다.

    .venv\Scripts\python.exe tests\e2e_pool.py
"""
import sys, re, io, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import TestServer, TEST_ADMIN_PW
import requests

ROOT = Path(__file__).resolve().parents[2]
ADMIN_PW = "Picc-Admin-2026!"
ok = fail = 0
def check(c, label, extra=""):
    global ok, fail
    if c: ok += 1; print(f'  [PASS] {label}')
    else: fail += 1; print(f'  [FAIL] {label}   {extra}')

with TestServer() as ts:
    B = ts.base
    admin = requests.Session()
    r = admin.post(f'{B}/login', data={'username':'admin','password':TEST_ADMIN_PW},
                   allow_redirects=True)
    if 'change-password' in r.url:
        admin.post(f'{B}/change-password', data={
            'current_password':TEST_ADMIN_PW,'new_password':ADMIN_PW,
            'confirm_password':ADMIN_PW}, allow_redirects=True)

    dcm = sorted((ROOT/'추가 data (20260112 이후 시술분)'/'Rt').glob('*.dcm'))
    ids = {'real': [], 'practice': []}
    for pool, files in (('real', dcm[:2]), ('practice', dcm[2:3])):
        for f in files:
            r = admin.post(f'{B}/admin/upload',
                           files={'files':(f.name, f.read_bytes())},
                           data={'pool':pool})
            j = r.json()
            assert j.get('ok'), r.text[:400]
            for res in j['results']:
                if res['status'] == 'ok': ids[pool].append(res['image_id'])

    print('=== 1. 업로드 pool 분리 ===')
    check(len(ids['real'])==2, '실제 2장 등재', ids)
    check(len(ids['practice'])==1, '연습 1장 등재', ids)
    r = admin.get(f'{B}/admin/upload')
    check('value="practice"' in r.text, '업로드 화면에 pool 선택 UI')

    print('=== 1b. 조용한 오배치 방지 (실제로 당한 사고) ===')
    # 사고 경위: 브라우저가 옛 upload.js 를 캐시해 pool 필드를 안 보냈고,
    # 서버가 기본값 'real' 로 처리해 연습용 영상이 전부 실제 pool 로 들어갔다.
    # 화면에는 아무 이상도 보이지 않았다. 세 층 모두에서 막는다.
    f = dcm[0]
    r = admin.post(f'{B}/admin/upload', files={'files':('x.dcm', f.read_bytes())})
    check(r.status_code == 400, 'pool 없이 업로드하면 거부(조용히 real 로 넣지 않음)',
          f'status={r.status_code}')
    check('Ctrl+F5' in r.text or '새로고침' in r.text,
          '거부 사유가 조치 방법까지 알려줌', r.text[:120])

    r = admin.get(f'{B}/static/upload.js')
    check('fd.append("pool"' in r.text, '서버가 주는 upload.js 가 pool 을 전송함')
    check(r.headers.get('cache-control') == 'no-cache',
          '정적 파일에 no-cache (브라우저가 옛 스크립트를 재검증 없이 못 씀)',
          r.headers.get('cache-control'))

    r = admin.get(f'{B}/admin/upload')
    check('?v=' in r.text, '정적 URL에 버전이 붙어 프록시 캐시도 갈림')

    print('=== 2. 관리자 집계는 실제 pool만 ===')
    r = admin.get(f'{B}/admin/export')
    m = re.search(r'전체 문제 \(?(\d+)', r.text) or re.search(r'>(\d+)</div>\s*<div class="stat-label">전체 문제', r.text)
    r2 = admin.get(f'{B}/admin')
    check(r2.status_code==200, '관리자 현황 렌더')

    # 계측자 생성
    r = admin.post(f'{B}/admin/users/create',
                   data={'username':'doc1','full_name':'테스트의사','role':'reviewer'},
                   allow_redirects=True)
    m = re.search(r"임시 비밀번호:\s*([A-Za-z0-9]+)", r.text)
    temp = m.group(1) if m else None
    check(bool(temp), '임시 비밀번호 발급', r.text[:200] if not temp else '')

    doc = requests.Session()
    r = doc.post(f'{B}/login', data={'username':'doc1','password':temp},
                 allow_redirects=True)
    if 'change-password' in r.url:
        doc.post(f'{B}/change-password', data={
            'current_password':temp,'new_password':'Doc1-pass-2026!',
            'confirm_password':'Doc1-pass-2026!'}, allow_redirects=True)

    print('=== 3. 기본은 실제 계측 모드 ===')
    r = doc.get(f'{B}/measure')
    check('연습 모드' not in r.text, '기본 모드에 연습 배너 없음')
    d = json.loads(re.search(r'<script id="measureData"[^>]*>(.*?)</script>', r.text, re.S).group(1))
    check(d['progress']['total'] == 2*8, '실제 모드 총 문제 = 2장×8', d['progress'])
    check(d['question']['imageId'] in ids['real'], '실제 pool 영상만 출제', d['question']['imageId'])

    print('=== 4. 연습 모드 전환 ===')
    r = doc.post(f'{B}/measure/mode/practice', allow_redirects=True)
    check('연습 모드' in r.text, '연습 배너 표시')
    d = json.loads(re.search(r'<script id="measureData"[^>]*>(.*?)</script>', r.text, re.S).group(1))
    check(d['progress']['total'] == 1*8, '연습 모드 총 문제 = 1장×8', d['progress'])
    check(d['question']['imageId'] in ids['practice'], '연습 pool 영상만 출제', d['question']['imageId'])

    print('=== 5. 연습 계측은 연구 데이터에 안 들어간다 ===')
    q = d['question']
    rr = doc.post(f'{B}/measure/answer', json={
        'image_id': q['imageId'], 'variable_key': q['variableKey'],
        'points': [[300.0,300.0],[1300.0,300.0]], 'duration_ms': 3000})
    check(rr.status_code==200 and rr.json()['ok'], '연습 계측 저장', rr.text[:200])
    ans = rr.json()['answers']
    check(len(ans)==1 and 'at' in ans[0], '완료목록에 완료시각 포함', ans[:1])
    check(ans[0].get('at') and re.match(r'^\d{2}:\d{2}:\d{2}$', ans[0]['at']),
          '완료시각 형식 HH:MM:SS', ans[0].get('at'))

    csv = admin.get(f'{B}/admin/export/long.csv').content.decode('utf-8-sig')
    check(str(ids['practice'][0]) not in [l.split(',')[0] for l in csv.splitlines()[1:]],
          'long CSV에 연습 영상 없음')
    check(len(csv.splitlines()) == 1, 'long CSV가 비어 있음(실제 계측 아직 0건)',
          f'{len(csv.splitlines())}행')

    print('=== 5b. 관리자 현황이 연습을 섞지 않는가 ===')
    # 실제로 있었던 버그: 분모(영상 수)는 실제 pool 인데 분자(계측 건수)는 pool 을
    # 걸러내지 않아, 연습 계측이 실제 진행률·중앙값·재측정 수로 잡혔다.
    # 이 시점 상태: 연습 계측 1건, 실제 계측 0건. 따라서 현황은 전부 0 이어야 한다.
    r = admin.get(f'{B}/admin')
    check(r.status_code == 200, '현황 렌더')
    check('연습 pool에 영상' in r.text, '현황에 연습 pool 존재가 표시됨')
    check('실제 계측 pool 기준' in r.text, '현황이 실제 계측 기준임을 명시')

    # 계측자별 진행 표에서 doc1 행만 떼어 낸다
    row = re.search(r'<tr>\s*<td>[^<]*<span class="muted small">doc1</span>\s*</td>'
                    r'(.*?)</tr>', r.text, re.S)
    check(row is not None, '계측자별 진행 표에서 doc1 행을 찾음')
    if row:
        cells = row.group(1)
        done = re.search(r'<td class="num">\s*(\d+)\s*/\s*(\d+)\s*</td>', cells)
        check(done is not None, '완료 칸 파싱', cells[:120])
        if done:
            check(done.group(1) == '0',
                  '연습 계측이 실제 완료 건수에 새지 않음',
                  f'완료={done.group(1)}/{done.group(2)} (연습 1건이 새면 1)')
        # 중앙값 칸 — 연습 소요시간이 새면 숫자가 찍힌다
        check('—' in cells,
              '연습 소요시간이 중앙값(가설 2 지표)에 새지 않음', cells[:200])

    csvx = admin.get(f'{B}/admin/export/long.csv').content.decode('utf-8-sig')
    check(len(csvx.splitlines()) == 1, '내보내기도 여전히 비어 있음',
          f'{len(csvx.splitlines())}행')

    print('=== 5c. 내보내기·사용자 화면도 연습을 섞지 않는가 ===')
    # 이 시점: 연습 계측 1건, 실제 계측 0건. 두 화면 모두 0 이어야 한다.
    r = admin.get(f'{B}/admin/export')
    check(r.status_code == 200, '내보내기 화면 렌더')
    stat = re.search(r'<div class="stat-value">(\d+)</div>\s*'
                     r'<div class="stat-label">유효 계측', r.text)
    check(stat is not None, '유효 계측 통계 파싱', r.text[:150])
    if stat:
        check(stat.group(1) == '0', '내보내기 화면의 유효 계측에 연습이 안 섞임',
              f'표시={stat.group(1)} (연습 1건이 새면 1)')
    paired = re.search(r'<div class="stat-value">(\d+)</div>\s*'
                       r'<div class="stat-label">2인 이상이 잰', r.text)
    if paired:
        check(paired.group(1) == '0', "'2인 이상이 잰' 수에 연습이 안 섞임",
              paired.group(1))

    r = admin.get(f'{B}/admin/users')
    check(r.status_code == 200, '사용자 화면 렌더')
    # doc1 행의 '계측' 칸(<td class="num">N</td>)만 정확히 집어낸다
    urow = re.search(r'doc1(.*?)</tr>', r.text, re.S)
    check(urow is not None, '사용자 표에서 doc1 행을 찾음')
    if urow:
        cnt = re.search(r'<td class="num">\s*(\d+)\s*</td>', urow.group(1))
        check(cnt is not None, '계측 건수 칸 파싱', urow.group(1)[:120])
        if cnt:
            check(cnt.group(1) == '0',
                  '사용자 화면 계측 건수에 연습이 안 섞임',
                  f'표시={cnt.group(1)} (연습 1건이 새면 1)')

    print('=== 6. 모드 전환해도 서로 안 섞인다 ===')
    r = doc.post(f'{B}/measure/mode/real', allow_redirects=True)
    d = json.loads(re.search(r'<script id="measureData"[^>]*>(.*?)</script>', r.text, re.S).group(1))
    check(len(d['answers'])==0, '실제 모드 완료목록에 연습 기록 없음', d['answers'])
    q = d['question']
    rr = doc.post(f'{B}/measure/answer', json={
        'image_id': q['imageId'], 'variable_key': q['variableKey'],
        'points': [[300.0,300.0],[1300.0,300.0]], 'duration_ms': 3000})
    check(len(rr.json()['answers'])==1, '실제 모드 완료목록 1건', rr.json()['answers'])

    csv = admin.get(f'{B}/admin/export/long.csv').content.decode('utf-8-sig')
    check(len(csv.splitlines())==2, 'long CSV에 실제 계측만 1건', f'{len(csv.splitlines())}행')

    print('=== 6b. 내 이력도 모드를 따라가는가 ===')
    # 이력이 pool 을 안 걸러 연습과 실제가 한 표에 섞였다.
    # 지금 상태: 연습 1건, 실제 1건. 각 모드에서 1건씩만 보여야 한다.
    doc.post(f'{B}/measure/mode/real', allow_redirects=True)
    r = doc.get(f'{B}/history')
    check(r.status_code == 200, '이력 렌더(실제 모드)')
    check('실제 계측 기록만' in r.text, '실제 모드임을 명시')
    rows_real = re.findall(r'<td class="mono small">\s*#(\d+)', r.text)
    check(len(rows_real) == 1, '실제 모드 이력은 1건',
          f'{len(rows_real)}건 {rows_real}')
    check(all(int(i) in ids['real'] for i in rows_real),
          '실제 모드 이력에 연습 영상이 없음', rows_real)

    doc.post(f'{B}/measure/mode/practice', allow_redirects=True)
    r = doc.get(f'{B}/history')
    check('연습 기록만' in r.text, '연습 모드임을 명시')
    rows_prac = re.findall(r'<td class="mono small">\s*#(\d+)', r.text)
    check(len(rows_prac) == 1, '연습 모드 이력은 1건',
          f'{len(rows_prac)}건 {rows_prac}')
    check(all(int(i) in ids['practice'] for i in rows_prac),
          '연습 모드 이력에 실제 영상이 없음', rows_prac)
    check(set(rows_real) != set(rows_prac), '두 모드가 서로 다른 기록을 보여줌')

    print('=== 7. 연습 기록 비우기 ===')
    doc.post(f'{B}/measure/mode/practice', allow_redirects=True)
    r = doc.post(f'{B}/measure/practice/clear', allow_redirects=True)
    d = json.loads(re.search(r'<script id="measureData"[^>]*>(.*?)</script>', r.text, re.S).group(1))
    check(len(d['answers'])==0, '연습 기록 삭제됨', d['answers'])
    doc.post(f'{B}/measure/mode/real', allow_redirects=True)
    r = doc.get(f'{B}/measure')
    d = json.loads(re.search(r'<script id="measureData"[^>]*>(.*?)</script>', r.text, re.S).group(1))
    check(len(d['answers'])==1, '실제 계측 기록은 보존됨', d['answers'])

    # (재측정 요청이 실제 계측 1건을 redo 로 바꾸므로, 그 1건이 남아 있어야
    #  하는 7번 뒤에 둔다.)
    print('=== 7b. 이력에서 행을 눌러 열람·재측정까지 ===')
    doc.post(f'{B}/measure/mode/real', allow_redirects=True)
    r = doc.get(f'{B}/history')
    mid = re.search(r'data-href="/measure\?review=(\d+)"', r.text)
    check(mid is not None, '이력 행에 열람 링크가 있음', r.text[:200])
    check('hist-row' in r.text, '행이 클릭 가능하게 표시됨')
    check('/measure?review=' in r.text, '링크가 계측 화면 열람 모드를 가리킴')

    if mid:
        m_id = int(mid.group(1))
        # 열람 모드로 화면이 열리는가
        r = doc.get(f'{B}/measure?review={m_id}')
        check(r.status_code == 200, '열람 모드 화면 렌더')
        d = json.loads(re.search(r'<script id="measureData"[^>]*>(.*?)</script>',
                                 r.text, re.S).group(1))
        check(d.get('reviewId') == m_id, 'reviewId 가 클라이언트로 전달됨',
              d.get('reviewId'))

        # 열람 API 가 그 계측을 돌려주는가
        rr = doc.get(f'{B}/measure/review/{m_id}').json()
        check(rr.get('ok') and rr['measurement_id'] == m_id, '열람 API 응답')
        check(rr.get('status') == 'done', '유효 상태로 열림', rr.get('status'))
        check(len(rr.get('points') or []) == 2, '계측 좌표 2점 반환',
              rr.get('points'))

        # 재측정 요청이 이력 경로에서도 동작하는가
        rd = doc.post(f'{B}/measure/redo', json={'measurement_id': m_id})
        check(rd.status_code == 200 and rd.json().get('ok'), '재측정 요청 성공',
              rd.text[:150])
        rr2 = doc.get(f'{B}/measure/review/{m_id}').json()
        check(rr2.get('status') == 'redo', '상태가 redo 로 바뀜', rr2.get('status'))

        # 이력에는 남아 있고(자료), 완료목록에서는 빠진다
        r = doc.get(f'{B}/history')
        check(f'review={m_id}' in r.text, '이력에는 그대로 남음(버려진 시도도 자료)')
        check('재측정 요청됨' in r.text, '이력에 재측정 상태가 표시됨')
        d = json.loads(re.search(r'<script id="measureData"[^>]*>(.*?)</script>',
                                 doc.get(f'{B}/measure').text, re.S).group(1))
        check(all(a['id'] != m_id for a in d['answers']),
              '완료목록에서는 빠짐')

    print('=== 8. 계측 화면 완료목록은 최근 20건까지 ===')
    # 150장×8 = 1,200문제라 목록이 끝없이 길어지면 찾는 용도로 쓸모가 없다.
    # 20을 넘기려면 문제가 20개보다 많아야 하므로 실제 pool 에 영상을 더 넣는다.
    # (이 단계는 앞 단계들의 전제를 깨므로 반드시 맨 마지막에 둔다.)
    for f in dcm[3:5]:
        a_r = admin.post(f'{B}/admin/upload',
                         files={'files': (f.name, f.read_bytes())},
                         data={'pool': 'real'})
        assert a_r.json().get('ok'), a_r.text[:300]

    doc.post(f'{B}/measure/mode/real', allow_redirects=True)
    last = None
    for _ in range(60):
        j = doc.get(f'{B}/measure/next').json()
        if j.get('done') or not j.get('question'):
            break
        q = j['question']
        last = doc.post(f'{B}/measure/answer', json={
            'image_id': q['imageId'], 'variable_key': q['variableKey'],
            'points': [[300.0, 300.0], [1300.0, 300.0]], 'duration_ms': 2500})
        if last.json()['progress']['done'] >= 23:
            break
    total_done = last.json()['progress']['done'] if last else 0
    check(total_done > 20, f'20건을 넘게 계측함({total_done}건)')
    check(len(last.json()['answers']) == 20,
          '응답의 완료목록이 20건으로 잘림', f"{len(last.json()['answers'])}건")

    r = doc.get(f'{B}/measure')
    d = json.loads(re.search(r'<script id="measureData"[^>]*>(.*?)</script>',
                             r.text, re.S).group(1))
    check(len(d['answers']) == 20, '화면 최초 로드에서도 20건',
          f"{len(d['answers'])}건")
    check(d.get('listLimit') == 20, 'listLimit 이 클라이언트로 전달됨',
          d.get('listLimit'))
    check(d['progress']['done'] == total_done,
          '총 건수는 목록 길이와 별개로 전달됨(20으로 잘리지 않음)',
          f"progress.done={d['progress']['done']}")
    check('내 이력</a> 메뉴를 사용하세요' in r.text, "'내 이력' 안내 문구가 있음")
    check('최근 <b>20건</b>만 표시' in r.text, '상한 건수가 안내에 표시됨')

    print('=== 9. 계측자 현황이 문제마다 갱신되는가 ===')
    # 로그인 시점 값에 굳어 있으면 동료 진행이 멈춘 것처럼 보인다.
    r = admin.post(f'{B}/admin/users/create',
                   data={'username': 'doc2', 'full_name': '테스트의사2',
                         'role': 'reviewer'}, allow_redirects=True)
    t2 = re.search(r"임시 비밀번호:\s*([A-Za-z0-9]+)", r.text)
    check(t2 is not None, 'doc2 생성')
    if t2:
        doc2 = requests.Session()
        rr = doc2.post(f'{B}/login',
                       data={'username': 'doc2', 'password': t2.group(1)},
                       allow_redirects=True)
        if 'change-password' in rr.url:
            doc2.post(f'{B}/change-password',
                      data={'current_password': t2.group(1),
                            'new_password': 'Doc2-pass-2026!',
                            'confirm_password': 'Doc2-pass-2026!'},
                      allow_redirects=True)

        doc.post(f'{B}/measure/mode/real', allow_redirects=True)
        j = doc.get(f'{B}/measure/next').json()
        check('peers' in j and len(j.get('peers') or []) == 2,
              '/measure/next 응답에 계측자 2명이 실려 옴',
              str(j.get('peers'))[:140])
        before = {x['name']: x['done'] for x in (j.get('peers') or [])}

        for _ in range(3):
            jj = doc2.get(f'{B}/measure/next').json()
            if jj.get('done') or not jj.get('question'):
                break
            q = jj['question']
            doc2.post(f'{B}/measure/answer', json={
                'image_id': q['imageId'], 'variable_key': q['variableKey'],
                'points': [[300.0, 300.0], [1300.0, 300.0]],
                'duration_ms': 2000})

        j2 = doc.get(f'{B}/measure/next').json()
        after = {x['name']: x['done'] for x in (j2.get('peers') or [])}
        check(after != before, 'doc2 의 진행이 doc1 쪽 peers 에 반영됨',
              f'{before} -> {after}')
        grew = [k for k in after if after[k] > before.get(k, -1)]
        check(len(grew) >= 1, '증가한 계측자가 있음', f'{before} -> {after}')

        q = j2.get('question')
        if q:
            ra = doc.post(f'{B}/measure/answer', json={
                'image_id': q['imageId'], 'variable_key': q['variableKey'],
                'points': [[300.0, 300.0], [1300.0, 300.0]],
                'duration_ms': 2000}).json()
            check(len(ra.get('peers') or []) == 2,
                  '/measure/answer 응답에도 peers 가 실려 옴',
                  str(ra.get('peers'))[:140])
            leaked = [k for x in (ra.get('peers') or []) for k in x
                      if k in ('value_mm', 'values', 'mm')]
            check(not leaked, 'peers 에 계측값이 새지 않음(건수만)', str(leaked))

print(f'\n{"="*50}\n통과 {ok} / 실패 {fail}')
sys.exit(1 if fail else 0)
