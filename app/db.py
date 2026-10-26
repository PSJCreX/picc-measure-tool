"""SQLAlchemy 엔진/세션 + 가벼운 컬럼 추가 마이그레이션.

`Base.metadata.create_all`은 테이블을 새로 만들 뿐 기존 테이블에 컬럼을 더해주지
않는다. 운영 중 스키마가 늘어날 때를 대비해 참조 구현(webapp/app/db.py)과 같은
`ensure_column` 헬퍼를 둔다.
"""
from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL

connect_args: dict = {}
if DATABASE_URL.startswith("sqlite"):
    # uvicorn 워커 스레드에서 같은 커넥션을 쓰므로 필요
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def enable_sqlite_wal() -> None:
    """WAL 모드 — 리뷰어 여러 명이 동시에 저장할 때 읽기가 막히지 않게."""
    if not DATABASE_URL.startswith("sqlite"):
        return
    with engine.begin() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA synchronous=NORMAL"))
        conn.execute(text("PRAGMA foreign_keys=ON"))


def ensure_column(table: str, column: str, ddl_type: str) -> None:
    """테이블에 컬럼이 없으면 추가한다. 있으면 아무것도 하지 않는다."""
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return
    names = {c["name"] for c in insp.get_columns(table)}
    if column in names:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


def run_migrations() -> None:
    """추가된 컬럼들을 기존 DB에 반영. 신규 설치에서는 전부 no-op."""
    ensure_column("images", "notes", "TEXT DEFAULT ''")
    ensure_column("images", "spacing_source", "VARCHAR(32) DEFAULT 'unknown'")
    ensure_column("measurements", "duration_ms", "INTEGER")
    ensure_column("measurements", "attempt", "INTEGER DEFAULT 1")
    ensure_column("measurements", "status", "VARCHAR(16) DEFAULT 'done'")
    ensure_column("users", "full_name", "VARCHAR(120) DEFAULT ''")
    # 연습/실제 pool 분리. 기존 영상은 전부 실제 계측용으로 남는다.
    ensure_column("images", "pool", "VARCHAR(16) DEFAULT 'real'")
