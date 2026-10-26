"""인증 — stdlib PBKDF2 해시 + 쿠키 세션.

컴파일 의존성(bcrypt/passlib)을 쓰지 않는다. 병원 PC에서 휠 빌드가 막히는 일이
잦고, ABDXAI 서버(`server/auth.py`)도 같은 이유로 `hashlib.pbkdf2_hmac`을 쓴다.
반복 횟수 600,000회는 OWASP 2023 PBKDF2-SHA256 권고치.

세션은 Starlette `SessionMiddleware`(서명된 쿠키)로 관리한다 — 별도 세션 테이블이
없어 서버 재시작(재부팅 자동 기동)에도 로그인이 유지된다. 서명 키는
`config.SECRET_KEY`가 파일에 보관한다.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16


def hash_password(plain: str) -> str:
    """`pbkdf2_sha256$<iters>$<salt_hex>$<hash_hex>` 형식으로 반환."""
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(
        "sha256", plain.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    """타이밍 공격에 안전하게 비교. 형식이 깨져 있으면 False."""
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        iters = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, iters)
    return hmac.compare_digest(dk, expected)


def needs_rehash(stored: str) -> bool:
    """반복 횟수가 현재 기준보다 낮으면 로그인 성공 시 재해시할 수 있게."""
    try:
        _, iters_s, _, _ = stored.split("$")
        return int(iters_s) < PBKDF2_ITERATIONS
    except ValueError:
        return True


def generate_temp_password(n: int = 10) -> str:
    """관리자가 계정을 만들거나 비밀번호를 초기화할 때 쓰는 임시 비밀번호.

    혼동하기 쉬운 글자(0/O, 1/l/I)를 뺀다 — 구두로 전달하는 경우가 많다.
    """
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))
