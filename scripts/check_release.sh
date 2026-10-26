#!/usr/bin/env bash
# 공개 전 스크럽 검사. 저장소 루트에서 실행한다.
#
#   bash scripts/check_release.sh
#
# FAIL 이 하나라도 있으면 푸시하지 않는다. WARN 은 사람이 판단한다.
set -uo pipefail
cd "$(dirname "$0")/.."

fail=0; warn=0
ok()   { printf '  \033[32m[OK]\033[0m   %s\n' "$1"; }
bad()  { printf '  \033[31m[FAIL]\033[0m %s\n' "$1"; fail=$((fail+1)); }
note() { printf '  \033[33m[WARN]\033[0m %s\n' "$1"; warn=$((warn+1)); }

# 검사 대상에서 제외할 것들 (git 메타, 가상환경, 데이터, 이 스크립트 자신)
scan() {
  grep -rIn -E "$1" . \
    --exclude-dir=.git --exclude-dir=.venv --exclude-dir=__pycache__ \
    --exclude-dir=data --exclude-dir=node_modules \
    --exclude='check_release.sh' --exclude='RELEASE_CHECKLIST.md' 2>/dev/null
}

echo "=== 1. 내부 네트워크 주소 ==="
hits=$(scan '(^|[^0-9.])(10\.3\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3}|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3})')
if [ -z "$hits" ]; then ok "원내망·사설망·Tailscale 주소 없음"
else bad "주소가 남아 있다:"; echo "$hits" | sed 's/^/        /'; fi

echo "=== 2. 사용자 절대경로 ==="
hits=$(scan '/home/[a-z_][a-z0-9_-]*/|/Users/[a-z_][a-z0-9_-]*/|[A-Z]:\\(Users|LOCAL-DEV|WIN-DEV-SERVER)')
if [ -z "$hits" ]; then ok "개인 홈·드라이브 경로 없음"
else bad "절대경로가 남아 있다:"; echo "$hits" | sed 's/^/        /'; fi

echo "=== 3. 비밀값 파일 ==="
for f in .env ACCOUNTS.local.md data/secret_key.txt data/app.db; do
  if [ -e "$f" ]; then bad "$f 가 작업트리에 있다 — 공개 복사본에 있어서는 안 된다"
  else ok "$f 없음"; fi
done
if ls ./*.local.md >/dev/null 2>&1; then bad "*.local.md 파일이 있다"; else ok "*.local.md 없음"; fi

echo "=== 4. git 추적 여부 ==="
if [ -d .git ]; then
  tracked=$(git ls-files | grep -E '^(\.env$|data/|.*\.local\.md$|.*\.pth$|.*\.dcm$|.*\.db$)' || true)
  if [ -z "$tracked" ]; then ok "비밀값·데이터·가중치가 추적되지 않음"
  else bad "추적되고 있다:"; echo "$tracked" | sed 's/^/        /'; fi
else note "git 저장소가 아직 아니다 — 추적 검사 생략"
fi

echo "=== 5. 평문 비밀번호로 보이는 기본값 ==="
hits=$(scan '(PASSWORD|SECRET|TOKEN|API_KEY)[ ]*=[ ]*["'"'"']?[A-Za-z0-9._-]{6,}')
if [ -z "$hits" ]; then ok "코드에 박힌 비밀값 없음"
else note "확인 필요 (테스트용 값은 무해):"; echo "$hits" | sed 's/^/        /'; fi

echo "=== 6. macOS 부산물 ==="
hits=$(find . -name '._*' -o -name '.DS_Store' | grep -v '\.git/' || true)
if [ -z "$hits" ]; then ok "AppleDouble·.DS_Store 없음"
else bad "정리 필요:"; echo "$hits" | sed 's/^/        /'; fi

echo "=== 7. 파이썬 구문 ==="
if python3 -m compileall -q app scripts tests docs >/dev/null 2>&1; then ok "전체 컴파일 통과"
else bad "컴파일 실패 — python3 -m compileall app scripts tests docs 로 확인"; fi

echo "=== 8. 공개 전 채워야 할 플레이스홀더 ==="
hits=$(scan '<(COPYRIGHT HOLDER|OWNER|REPO|FAMILY NAME|GIVEN NAMES|AFFILIATION)>|0000-0000-0000-0000')
if [ -z "$hits" ]; then ok "플레이스홀더 없음"
else note "아직 비어 있다:"; echo "$hits" | sed 's/^/        /'; fi

echo "=== 9. 폰트 라이선스 ==="
if ls static/fonts/*.woff2 >/dev/null 2>&1; then
  if [ -f static/fonts/OFL.txt ]; then ok "OFL.txt 있음"
  else note "폰트를 함께 배포하는데 static/fonts/OFL.txt 가 없다 (OFL-1.1 요구사항)"; fi
else ok "폰트를 배포하지 않음"
fi

echo
printf 'FAIL %d · WARN %d\n' "$fail" "$warn"
[ "$fail" -eq 0 ] || { echo "→ FAIL 을 해결하기 전에는 공개하지 않는다."; exit 1; }
echo "→ FAIL 없음. WARN 을 확인하고 공개 여부를 판단한다."
