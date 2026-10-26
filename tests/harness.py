"""테스트 전용 서버를 격리해서 띄우는 하네스.

**왜 필요한가 — 사고 기록**

처음엔 스모크 테스트를 운영 서버(포트 1531)에 그대로 물려 돌렸다.
그 테스트의 마지막 단계가 '초기화 안전장치' 검증이라 `/admin/reset`을 **실제로**
호출했고, 그 결과 **그 시각 계측 중이던 사람의 기록이 전부 지워졌다.**

테스트가 지울 수 있는 데이터가 곧 사람이 만든 데이터인 구조 자체가 잘못이었다.
그래서 이제 테스트는 언제나

  * 임시 폴더(DATA_DIR)를 새로 파고
  * 거기에 자기 DB·영상 저장소를 만들고
  * 다른 포트로 서버를 따로 띄우고
  * 끝나면 프로세스를 죽이고 폴더를 지운다.

운영 DB는 손도 대지 못한다. `--live` 를 명시하지 않는 한
운영 포트로는 아예 붙지 않는다.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
PYTHON = PROJECT / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():                       # 비-Windows 대비
    PYTHON = PROJECT / ".venv" / "bin" / "python"

TEST_ADMIN_PW = "picc-admin-2026"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_healthy(base: str, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/healthz", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.4)
    return False


class TestServer:
    """임시 DATA_DIR 위에서 도는 일회용 서버. with 문으로 쓴다."""

    def __init__(self) -> None:
        self.data_dir = Path(tempfile.mkdtemp(prefix="picc_test_"))
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None
        self.log = self.data_dir / "server.log"

    def __enter__(self) -> "TestServer":
        env = dict(os.environ)
        # load_dotenv 는 이미 설정된 환경변수를 덮어쓰지 않으므로
        # 여기 넣은 값이 .env 보다 우선한다.
        env["DATA_DIR"] = str(self.data_dir)
        env["WEBAPP_PORT"] = str(self.port)
        env["WEBAPP_HOST"] = "127.0.0.1"
        env["INITIAL_ADMIN_USERNAME"] = "admin"
        env["INITIAL_ADMIN_PASSWORD"] = TEST_ADMIN_PW
        env["SECRET_KEY"] = "test-only-key"

        out = open(self.log, "wb")
        self.proc = subprocess.Popen(
            [str(PYTHON), "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(self.port),
             "--log-level", "warning"],
            cwd=str(PROJECT), env=env, stdout=out, stderr=subprocess.STDOUT,
        )
        if not _wait_healthy(self.base):
            self.__exit__(None, None, None)
            raise RuntimeError(
                f"테스트 서버가 뜨지 않았습니다. 로그: {self.log}"
            )
        return self

    def __exit__(self, *exc) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
        # 실패했을 땐 로그를 남겨 두는 편이 낫다
        if all(e is None for e in exc):
            shutil.rmtree(self.data_dir, ignore_errors=True)
        else:
            print(f"\n서버 로그를 남겨 둡니다: {self.log}", file=sys.stderr)


def guard_live_target(base: str, allow_live: bool) -> None:
    """운영 서버를 향해 파괴적 테스트를 돌리려는 시도를 막는다."""
    if allow_live:
        print("!! --live 지정됨 — 이 서버의 계측 기록이 지워집니다 !!",
              file=sys.stderr)
        return
    raise SystemExit(
        f"거부: {base} 는 운영 서버입니다.\n"
        "이 테스트는 마지막에 /admin/reset 을 실제로 호출해 계측 기록을 지웁니다.\n"
        "인자 없이 실행하면 임시 DB로 서버를 따로 띄워 안전하게 돌아갑니다."
    )
