import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS watermark_templates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  column_name TEXT NOT NULL,
  version_no INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('现行','停用')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS run_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  run_code TEXT NOT NULL UNIQUE,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待盖印','已盖印','已作废')),
  watermark_value TEXT,
  stamped_at TEXT,
  voided_at TEXT,
  void_reason TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lineage_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  upstream_run_code TEXT NOT NULL,
  downstream_run_code TEXT NOT NULL,
  UNIQUE(upstream_run_code, downstream_run_code)
);

CREATE TABLE IF NOT EXISTS gap_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  expected_count INTEGER NOT NULL,
  stamped_count INTEGER NOT NULL,
  gap_count INTEGER NOT NULL,
  summary TEXT NOT NULL,
  seq INTEGER,
  reading TEXT,
  void_codes TEXT,
  void_count INTEGER NOT NULL DEFAULT 0,
  digest TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS void_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reason TEXT NOT NULL,
  void_codes TEXT NOT NULL,
  void_count INTEGER NOT NULL,
  digest TEXT NOT NULL,
  captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gap_verifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_id INTEGER NOT NULL,
  round1_gap INTEGER NOT NULL,
  round2_gap INTEGER NOT NULL,
  aligned INTEGER NOT NULL,
  detail TEXT NOT NULL,
  reading TEXT,
  seq INTEGER,
  void_count INTEGER,
  checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orchestration_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_code TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  report_id INTEGER,
  reading TEXT,
  seq INTEGER,
  status TEXT NOT NULL,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

SEED_TEMPLATES = [
    ("dw_orders_d", "wm_batch", 1, "现行"),
    ("dw_payments_d", "wm_batch", 1, "现行"),
    ("dm_gmv_d", "wm_trace", 1, "现行"),
]

SEED_BATCHES = [
    ("dw_orders_d", "ord-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_payments_d", "pay-20261114-01", "2026-11-14", "待盖印", None),
    ("dm_gmv_d", "gmv-20261114-01", "2026-11-14", "待盖印", None),
    ("dw_orders_d", "ord-20261113-01", "2026-11-13", "已盖印", "WM-ORD-131"),
]

SEED_EDGES = [
    ("ord-20261114-01", "gmv-20261114-01"),
    ("pay-20261114-01", "gmv-20261114-01"),
    ("ord-20261113-01", "gmv-20261114-01"),
]


def _migrate(conn) -> None:
    """旧库补齐新增列，不清存量数据。"""
    cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(run_batches)").fetchall()
    }
    for name, ddl in (
        ("voided_at", "ALTER TABLE run_batches ADD COLUMN voided_at TEXT"),
        ("void_reason", "ALTER TABLE run_batches ADD COLUMN void_reason TEXT"),
    ):
        if name not in cols:
            conn.execute(ddl)
    rcols = {
        r["name"] for r in conn.execute("PRAGMA table_info(gap_reports)").fetchall()
    }
    for name, ddl in (
        ("seq", "ALTER TABLE gap_reports ADD COLUMN seq INTEGER"),
        ("reading", "ALTER TABLE gap_reports ADD COLUMN reading TEXT"),
        ("void_codes", "ALTER TABLE gap_reports ADD COLUMN void_codes TEXT"),
        (
            "void_count",
            "ALTER TABLE gap_reports ADD COLUMN void_count INTEGER NOT NULL DEFAULT 0",
        ),
        ("digest", "ALTER TABLE gap_reports ADD COLUMN digest TEXT"),
    ):
        if name not in rcols:
            conn.execute(ddl)
    vcols = {
        r["name"] for r in conn.execute("PRAGMA table_info(gap_verifications)").fetchall()
    }
    for name, ddl in (
        ("reading", "ALTER TABLE gap_verifications ADD COLUMN reading TEXT"),
        ("seq", "ALTER TABLE gap_verifications ADD COLUMN seq INTEGER"),
        ("void_count", "ALTER TABLE gap_verifications ADD COLUMN void_count INTEGER"),
    ):
        if name not in vcols:
            conn.execute(ddl)
    # 序号唯一是“两边同时抢重扫不互相盖摘要”的最后一道闸；
    # 旧库的存量行 seq 为 NULL，SQLite 唯一索引允许多个 NULL。
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_gap_reports_seq ON gap_reports(seq)"
    )


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        row = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        if row:
            return
        for table, col, ver, status in SEED_TEMPLATES:
            conn.execute(
                "INSERT INTO watermark_templates(table_name, column_name, version_no, status) "
                "VALUES (?,?,?,?)",
                (table, col, ver, status),
            )
        for table, code, biz, status, wm in SEED_BATCHES:
            conn.execute(
                "INSERT INTO run_batches(table_name, run_code, biz_date, status, watermark_value, stamped_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    table,
                    code,
                    biz,
                    status,
                    wm,
                    "2026-11-13 18:00:00" if status == "已盖印" else None,
                ),
            )
        for up, down in SEED_EDGES:
            conn.execute(
                "INSERT INTO lineage_edges(upstream_run_code, downstream_run_code) VALUES (?,?)",
                (up, down),
            )
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
