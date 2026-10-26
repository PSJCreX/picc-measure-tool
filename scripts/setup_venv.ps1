# PICC 웹 계측 라벨링 — 가상환경 생성 및 의존성 설치
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup_venv.ps1
#
# 이 PC에는 torch/cv2/pydicom이 설치된 파이썬 환경이 없다(프로젝트 CLAUDE.md §10).
# 웹 툴은 추론과 무관하므로 torch 없이 가벼운 전용 venv를 따로 만든다.

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvDir = Join-Path $ProjectRoot '.venv'
$Python = Join-Path $VenvDir 'Scripts\python.exe'

Write-Output "프로젝트: $ProjectRoot"

# 파이썬 실행기 탐색: py -3.12 → py -3.10 → python
$launcher = $null
foreach ($cand in @(@('py', '-3.12'), @('py', '-3.10'), @('python'))) {
    try {
        $exe = $cand[0]
        $args = if ($cand.Count -gt 1) { @($cand[1], '--version') } else { @('--version') }
        $out = & $exe @args 2>&1
        if ($LASTEXITCODE -eq 0) {
            $launcher = $cand
            Write-Output "파이썬 발견: $($cand -join ' ')  ($out)"
            break
        }
    } catch { }
}
if (-not $launcher) { throw "파이썬을 찾을 수 없습니다. python.org에서 3.12를 설치하세요." }

if (-not (Test-Path $Python)) {
    Write-Output "가상환경 생성: $VenvDir"
    $exe = $launcher[0]
    if ($launcher.Count -gt 1) {
        & $exe $launcher[1] -m venv $VenvDir
    } else {
        & $exe -m venv $VenvDir
    }
    if ($LASTEXITCODE -ne 0) { throw "venv 생성 실패" }
} else {
    Write-Output "기존 가상환경 사용: $VenvDir"
}

Write-Output "pip 업그레이드..."
& $Python -m pip install --upgrade pip setuptools wheel --quiet
if ($LASTEXITCODE -ne 0) { throw "pip 업그레이드 실패" }

Write-Output "의존성 설치 (수 분 소요)..."
& $Python -m pip install -r (Join-Path $ProjectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw "의존성 설치 실패" }

# JPEG2000 디코더 확인 — test set의 94%가 J2K라 이게 없으면 업로드가 전부 실패한다
Write-Output ""
Write-Output "디코더 확인..."
& $Python -c @"
import pydicom, sys
ok = True
for mod, label in (('openjpeg','pylibjpeg-openjpeg (JPEG2000)'),
                   ('libjpeg','pylibjpeg-libjpeg (JPEG)'),
                   ('gdcm','python-gdcm (fallback)')):
    try:
        __import__(mod); print('  [OK] ' + label)
    except Exception as e:
        print('  [--] ' + label + ' : ' + str(e)); ok = False
print('pydicom ' + pydicom.__version__)
sys.exit(0 if ok else 1)
"@
if ($LASTEXITCODE -ne 0) {
    Write-Warning "일부 디코더가 없습니다. JPEG2000 DICOM 업로드가 실패할 수 있습니다."
}

# .env 초기 생성
$EnvFile = Join-Path $ProjectRoot '.env'
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $ProjectRoot '.env.example') $EnvFile
    Write-Output ""
    Write-Output ".env를 생성했습니다. 초기 관리자 계정을 확인/수정하세요: $EnvFile"
}

Write-Output ""
Write-Output "완료. 서버 실행:"
Write-Output "  powershell -ExecutionPolicy Bypass -File scripts\start_server.ps1"
Write-Output "부팅 시 자동 기동 등록(관리자 권한 PowerShell):"
Write-Output "  powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1"
