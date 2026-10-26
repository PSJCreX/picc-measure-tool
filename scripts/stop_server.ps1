# PICC 웹 계측 라벨링 서버 정지.
#
#   powershell -ExecutionPolicy Bypass -File scripts\stop_server.ps1
#
# 왜 전용 스크립트가 필요한가
# ---------------------------
# Windows venv의 `.venv\Scripts\python.exe` 는 얇은 런처라, 실행하면 **베이스
# 인터프리터를 자식 프로세스로 다시 띄운다.** 실제로 포트를 잡는 쪽은 그 자식이고
# 경로는 `C:\...\Python312\python.exe` 로 찍힌다.
#
# 그래서 "실행 파일 경로가 web_labeling 인 python 을 죽인다" 는 식의 정리는
# 껍데기(부모)만 죽이고 **정작 서버는 살려 둔다.** 포트 소유자를 찾아서 죽여야 한다.
#
# 예약 작업(PICC-Labeling-Server)이 5분마다 상태를 확인하므로, 잠시 뒤 자동으로
# 다시 뜬다. 완전히 멈추려면 -DisableTask 를 함께 준다.

param(
    [switch]$DisableTask
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TaskName = 'PICC-Labeling-Server'

# .env 에서 포트 읽기
$ListenPort = 1531
$EnvFile = Join-Path $ProjectRoot '.env'
if (Test-Path $EnvFile) {
    foreach ($line in Get-Content $EnvFile) {
        if ($line -match '^\s*WEBAPP_PORT\s*=\s*(\d+)\s*$') { $ListenPort = [int]$Matches[1] }
    }
}

if ($DisableTask) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        Write-Output "예약 작업 비활성화: $TaskName  (다시 켜기: Enable-ScheduledTask -TaskName $TaskName)"
    }
}

$conns = Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction SilentlyContinue
if (-not $conns) {
    Write-Output "포트 $ListenPort 에서 대기 중인 프로세스가 없습니다."
    exit 0
}

# 예약 작업은 SYSTEM 계정으로 서버를 띄운다. 승격되지 않은 세션에서는 그 프로세스를
# 죽일 수 없는데, -ErrorAction SilentlyContinue 로 삼켜 버리면 "정지: PID ..." 만
# 찍히고 서버는 멀쩡히 살아 있다. 실패를 실패라고 말하게 하려고 결과를 확인한다.
$denied = $false

foreach ($c in $conns) {
    $serverPid = $c.OwningProcess
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$serverPid" -ErrorAction SilentlyContinue
    if (-not $proc) { continue }

    # 조상 사슬을 **끝까지** 거슬러 올라가며 정리한다.
    #
    # --reload 로 띄우면 계층이 하나 늘어난다:
    #     venv 런처 stub → uvicorn 리로더(부모) → 실제 서버(포트 보유)
    # 한 단계만 죽이면 리로더가 살아남아 워커를 즉시 되살린다. 그래서 포트를 쥔
    # 프로세스부터 시작해, uvicorn 명령줄을 가진 python 조상을 전부 모아 둔다.
    $chain = @($serverPid)
    $cursor = $proc
    for ($depth = 0; $depth -lt 4; $depth++) {
        $ppid = $cursor.ParentProcessId
        if (-not $ppid) { break }
        $anc = Get-CimInstance Win32_Process -Filter "ProcessId=$ppid" -ErrorAction SilentlyContinue
        if (-not $anc) { break }
        # uvicorn 워커의 조상은 감시자(serve.py) → venv 런처 stub 이다.
        $isOurs = $anc.Name -eq 'python.exe' -and
                  ($anc.CommandLine -like '*uvicorn*' -or $anc.CommandLine -like '*serve.py*')
        if (-not $isOurs) { break }
        $chain += $ppid
        $cursor = $anc
    }

    # 조상(리로더)을 먼저 죽인다 — 자식부터 죽이면 그 사이에 재기동된다.
    [array]::Reverse($chain)
    foreach ($target in $chain) {
        $t = Get-CimInstance Win32_Process -Filter "ProcessId=$target" -ErrorAction SilentlyContinue
        if (-not $t) { continue }
        Write-Output "정지 시도: PID $target ($($t.Name))"
        try {
            Stop-Process -Id $target -Force -ErrorAction Stop
        } catch {
            $denied = $true
            Write-Warning "PID $target 를 종료하지 못했습니다: $($_.Exception.Message)"
        }
    }
}

Start-Sleep -Seconds 1
$still = Get-NetTCPConnection -LocalPort $ListenPort -State Listen -ErrorAction SilentlyContinue
if ($still) {
    Write-Warning "포트 $ListenPort 가 아직 잡혀 있습니다 (PID $($still[0].OwningProcess))."

    $me = New-Object Security.Principal.WindowsPrincipal(
              [Security.Principal.WindowsIdentity]::GetCurrent())
    $elevated = $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

    if (-not $elevated) {
        Write-Output ""
        Write-Output "── 원인: 관리자 권한이 없습니다 ──────────────────────────────"
        Write-Output "예약 작업(PICC-Labeling-Server)이 서버를 SYSTEM 계정으로 띄웠기 때문에,"
        Write-Output "일반 권한 PowerShell 에서는 그 프로세스를 종료할 수 없습니다."
        Write-Output ""
        Write-Output "PowerShell 을 '관리자 권한으로 실행' 한 뒤 다시 돌리세요:"
        Write-Output "  powershell -ExecutionPolicy Bypass -File scripts\stop_server.ps1"
        Write-Output "──────────────────────────────────────────────────────────────"
        exit 1
    }
    if ($denied) { exit 1 }
} else {
    Write-Output "포트 $ListenPort 해제됨."
}

if (-not $DisableTask) {
    Write-Output ""
    Write-Output "예약 작업이 5분 내에 서버를 다시 띄웁니다."
    Write-Output "완전히 멈추려면:  -DisableTask 옵션을 함께 사용하세요."
}
