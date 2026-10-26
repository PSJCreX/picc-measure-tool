"""uvicorn 감시자 — `app/` 의 파이썬 소스가 바뀌면 서버를 다시 띄운다.

왜 uvicorn `--reload` 를 안 쓰는가
---------------------------------
Windows에서는 동작하지 않는다. `uvicorn/supervisors/basereload.py` 의 `restart()` 는

    if sys.platform == "win32":
        os.kill(self.process.pid, signal.CTRL_C_EVENT)
    else:
        self.process.terminate()
    self.process.join()

인데, Windows의 `CTRL_C_EVENT` 는 **특정 PID로 보낼 수 없다**(콘솔 그룹 전체에만
전달된다). 그래서 워커가 죽지 않고 `join()` 이 영원히 대기한다. 실제로 확인해 보면
`WatchFiles detected changes ... Reloading...` 까지만 찍히고 `Started server process`
가 다시 나오지 않으며, 옛 워커가 계속 응답한다. 출력 리다이렉트/새 콘솔/새 프로세스
그룹 세 가지 기동 방식에서 모두 같았다.

왜 이게 필요한가
----------------
예약 작업(`PICC-Labeling-Server`)이 서버를 **SYSTEM 계정**으로 띄우기 때문에,
승격되지 않은 세션에서는 그 프로세스를 죽일 수 없다(Access denied). 코드를 고칠
때마다 관리자 PowerShell을 새로 띄워야 하는 건 사람에게도 번거롭고 자동화
세션에서는 아예 불가능하다.

이 감시자는 그 경계를 우회하지 않고 **없앤다** — uvicorn을 자기 자식으로 띄우므로,
어떤 계정으로 돌든 자기 자식은 항상 종료할 수 있다.

감시 범위
---------
`app/**/*.py` 만 본다. 이유가 각각 있다.

* `templates/` — Jinja `auto_reload=True` 라 재기동 없이 이미 반영된다.
* `static/`    — 요청마다 디스크에서 읽으므로 재기동이 의미 없다.
* `data/`      — **절대 보면 안 된다.** SQLite WAL이 계측할 때마다 바뀌므로
                 감시 대상에 넣으면 서버가 끝없이 재기동한다.

실행
----
    .venv\\Scripts\\python.exe scripts\\serve.py --host 0.0.0.0 --port 1531
    ...  --no-watch      감시 없이 그냥 띄우기만
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
WATCH_DIR = PROJECT / "app"
PYTHON = PROJECT / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():                       # 비-Windows 대비
    PYTHON = PROJECT / ".venv" / "bin" / "python"

# 편집이 연달아 일어날 때 매번 재기동하지 않도록 잠잠해질 때까지 기다리는 시간.
QUIET_SECONDS = 1.2
# 파일 검사 주기.
POLL_SECONDS = 0.6
# 자식이 스스로 죽었을 때(예: 문법 오류) 다시 띄우기까지의 대기. 반복되면 늘린다.
RESPAWN_MIN = 2.0
RESPAWN_MAX = 30.0
# 종료 요청 후 강제 종료까지 기다리는 시간.
TERM_GRACE = 8.0

_stop = threading.Event()


def _make_kill_on_close_job():
    """자식이 **부모와 함께 반드시 죽도록** 묶어 두는 Job Object를 만든다.

    왜 필요한가 — Windows의 `TerminateProcess` 는 대상의 `finally` 를 실행시키지
    않는다. 그래서 감시자가 강제 종료되면(stop 스크립트, 작업 관리자, 크래시)
    `server.stop()` 이 돌지 못하고 uvicorn 자식이 **고아로 남아 포트를 계속 문다.**
    그 상태에서는 다시 띄울 수도 없다.

    `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 를 걸어 두면, 감시자가 어떤 경로로 죽든
    마지막 핸들이 닫히는 순간 커널이 자식을 정리한다. 파이썬 코드가 개입할 필요가
    없으므로 강제 종료에도 새지 않는다.

    실패하면 None을 돌려준다 — 그때는 평소의 terminate 경로로만 동작한다.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        JobObjectExtendedLimitInformation = 9

        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        ):
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception as exc:                          # noqa: BLE001
        log(f"Job Object 생성 실패(무시하고 계속): {exc}")
        return None


def _assign_to_job(job, proc: subprocess.Popen) -> None:
    """자식을 Job에 넣는다. 실패해도 서버 기동 자체는 막지 않는다."""
    if job is None or os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = int(proc._handle)                    # type: ignore[attr-defined]
        if not kernel32.AssignProcessToJobObject(job, handle):
            log(f"Job 등록 실패 PID {proc.pid} (err={ctypes.get_last_error()})")
    except Exception as exc:                          # noqa: BLE001
        log(f"Job 등록 중 예외(무시): {exc}")


def _force_utf8_output() -> None:
    """로그 출력을 UTF-8 로 고정한다.

    런처가 stdout 을 파일로 리다이렉트하면 Windows 는 콘솔 코드페이지(cp949)로
    인코딩한다. 한글은 cp949 에 있지만 em dash( — ) 같은 문자는 없어서
    `UnicodeEncodeError` 로 **프로세스가 그대로 죽는다.** 실제로 중계(relay.py)가
    이것 때문에 기동 직후 죽어 서비스가 멈췄다. 로그 한 줄로 서비스가 내려가서는
    안 되므로 여기서도 같이 막는다.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def log(msg: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[serve {stamp}] {msg}", flush=True)


def snapshot() -> dict[str, tuple[float, int]]:
    """감시 대상의 (수정시각, 크기). 내용 해시보다 싸고 충분하다."""
    out: dict[str, tuple[float, int]] = {}
    if not WATCH_DIR.exists():
        return out
    for p in WATCH_DIR.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        out[str(p)] = (st.st_mtime, st.st_size)
    return out


class Server:
    """uvicorn 자식 프로세스 하나."""

    def __init__(self, argv: list[str], job=None) -> None:
        self.argv = argv
        self.job = job
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        kwargs: dict = {}
        if os.name == "nt":
            # 자기 프로세스 그룹을 갖게 해 둔다. 부모 콘솔의 Ctrl+C가 자식에게
            # 새어 들어가지 않아, 종료 시점을 이쪽에서만 통제할 수 있다.
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        self.proc = subprocess.Popen(
            self.argv, cwd=str(PROJECT), **kwargs
        )
        # 감시자가 강제 종료돼도 자식이 고아로 남지 않게 커널에 맡긴다.
        _assign_to_job(self.job, self.proc)
        log(f"uvicorn 기동 PID {self.proc.pid}")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        """자식을 확실히 내린다.

        uvicorn과 달리 신호에 기대지 않는다 — `terminate()`(Windows에서는
        TerminateProcess)는 **자기 자식**에게는 계정과 무관하게 통한다.
        DB는 WAL 모드라 갑작스러운 종료에도 안전하다.
        """
        if self.proc is None or self.proc.poll() is not None:
            return
        pid = self.proc.pid
        try:
            self.proc.terminate()
        except OSError as exc:
            log(f"terminate 실패 PID {pid}: {exc}")
        try:
            self.proc.wait(timeout=TERM_GRACE)
            log(f"uvicorn 종료 PID {pid}")
            return
        except subprocess.TimeoutExpired:
            log(f"PID {pid} 가 {TERM_GRACE}초 내에 끝나지 않아 강제 종료합니다.")
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception as exc:                      # noqa: BLE001
            log(f"kill 실패 PID {pid}: {exc}")


def build_uvicorn_argv(host: str, port: int, log_level: str) -> list[str]:
    return [
        str(PYTHON), "-m", "uvicorn", "app.main:app",
        "--host", host, "--port", str(port), "--log-level", log_level,
    ]


def _detach_from_console() -> None:
    """부모 콘솔에서 떨어져 나온다 — 서비스는 콘솔의 운명에 묶이면 안 된다.

    왜 필요한가(실제로 당한 일) — 승격된 PowerShell에서 런처를 돌려 감시자를
    띄웠는데, 그 PowerShell 창이 Ctrl+C(`STATUS_CONTROL_C_EXIT`)로 끝나자
    **콘솔 그룹 전체에 CTRL_C_EVENT가 퍼져** 감시자까지 같이 죽었다.
    감시자가 죽으니 Job Object가 닫히며 uvicorn도 함께 정리되어 서버가 내려갔다.

    콘솔을 떼어 내면 어떤 콘솔 이벤트도 도달하지 못한다. 종료는 오직
    `Stop-Process`(TerminateProcess)로만 이뤄지고, 그때는 Job Object가 자식을
    정리한다 — 의도한 경로만 남는다.

    stdout/stderr 가 파일로 리다이렉트된 상태라면 그 핸들은 콘솔과 무관하므로
    로그는 계속 남는다. 터미널에서 직접 띄워 Ctrl+C로 다루고 싶을 때를 위해,
    tty 에 붙어 있으면 떼어 내지 않는다.
    """
    if os.name != "nt":
        return
    try:
        if sys.stdout is not None and sys.stdout.isatty():
            log("콘솔에 붙어 실행 중 — 분리하지 않습니다(Ctrl+C로 종료 가능).")
            return
    except Exception:                                 # noqa: BLE001
        pass
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if kernel32.FreeConsole():
            log("부모 콘솔에서 분리했습니다 (콘솔 종료에 휘말리지 않음).")
    except Exception as exc:                          # noqa: BLE001
        log(f"콘솔 분리 실패(무시): {exc}")


def _install_signal_handlers() -> None:
    def handler(signum, _frame):                      # noqa: ANN001
        log(f"종료 신호({signum}) — 정리하고 나갑니다.")
        _stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass
    if os.name == "nt" and hasattr(signal, "SIGBREAK"):
        try:
            signal.signal(signal.SIGBREAK, handler)
        except (ValueError, OSError):
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="uvicorn 감시자")
    ap.add_argument("--host", default=os.environ.get("WEBAPP_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("WEBAPP_PORT", "1531")))
    ap.add_argument("--log-level", default="info")
    ap.add_argument("--no-watch", action="store_true",
                    help="감시 없이 그냥 띄우기만 한다.")
    args = ap.parse_args()

    # 순서 중요: 콘솔을 먼저 떼어 낸 뒤 핸들러를 건다.
    _force_utf8_output()
    _detach_from_console()
    _install_signal_handlers()

    job = _make_kill_on_close_job()
    if job is None and os.name == "nt":
        log("경고: Job Object를 못 만들었습니다. 감시자가 강제 종료되면 "
            "uvicorn 자식이 남을 수 있습니다.")
    server = Server(build_uvicorn_argv(args.host, args.port, args.log_level), job)
    server.start()

    if args.no_watch:
        log("감시 꺼짐 — 자식이 끝날 때까지 기다립니다.")
        try:
            while not _stop.is_set() and server.alive():
                time.sleep(POLL_SECONDS)
        finally:
            server.stop()
        return 0

    log(f"감시 중: {WATCH_DIR}  (*.py)")
    known = snapshot()
    pending_since: float | None = None
    respawn_delay = RESPAWN_MIN
    crashed_at: float | None = None

    try:
        while not _stop.is_set():
            time.sleep(POLL_SECONDS)

            # 1) 자식이 스스로 죽었으면(문법 오류 등) 다시 띄운다.
            if not server.alive():
                if crashed_at is None:
                    code = server.proc.returncode if server.proc else "?"
                    log(f"자식이 종료됨(code={code}). {respawn_delay:.0f}초 후 재기동합니다.")
                    crashed_at = time.monotonic()
                elif time.monotonic() - crashed_at >= respawn_delay:
                    server.start()
                    crashed_at = None
                    # 계속 죽으면 간격을 늘려 로그와 CPU를 아낀다.
                    respawn_delay = min(RESPAWN_MAX, respawn_delay * 2)
                # 죽어 있는 동안에도 파일 변경은 계속 추적한다 —
                # 고치자마자 즉시 살아나야 한다.
                current = snapshot()
                if current != known:
                    known = current
                    log("변경 감지 — 즉시 재기동합니다.")
                    server.start()
                    crashed_at = None
                    respawn_delay = RESPAWN_MIN
                continue

            respawn_delay = RESPAWN_MIN

            # 2) 변경 감지 → 잠잠해질 때까지 기다렸다가 한 번만 재기동
            current = snapshot()
            if current != known:
                if pending_since is None:
                    changed = sorted(
                        set(current) ^ set(known)
                        or {k for k in current if known.get(k) != current[k]}
                    )
                    names = ", ".join(Path(c).name for c in list(changed)[:4])
                    log(f"변경 감지: {names or '(내용 변경)'}")
                known = current
                pending_since = time.monotonic()
                continue

            if pending_since is not None and \
                    time.monotonic() - pending_since >= QUIET_SECONDS:
                pending_since = None
                log("재기동합니다.")
                server.stop()
                server.start()
    finally:
        server.stop()
        log("감시자 종료.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
