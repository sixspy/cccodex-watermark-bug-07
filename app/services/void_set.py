import hashlib
from datetime import datetime, timedelta, timezone

from app.db import get_db
from app.services.errors import ServiceError

# 作废集合快照在页面打开时冻结；快照只存 run_code 明细，
# 计数与摘要都由明细推出，事后任何一处对不上都能立刻发现。
# 快照不是可编辑的“配置”，只是一份读数依据。


def digest_of(codes: list[str]) -> str:
    joined = "\n".join(sorted(codes))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _now_text() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def read_void_codes(conn) -> list[str]:
    """在调用方给出的连接/事务里读作废批实物，保证与计数同源。"""
    rows = conn.execute(
        "SELECT run_code FROM run_batches WHERE status='已作废' ORDER BY run_code"
    ).fetchall()
    return [r["run_code"] for r in rows]


def capture_snapshot(reason: str = "打开缺口对账页", db_path: str | None = None) -> dict:
    """打开差额目录时冻结一份作废集合快照。"""
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            codes = read_void_codes(conn)
            digest = digest_of(codes)
            captured_at = _now_text()
            cur = conn.execute(
                "INSERT INTO void_snapshots(reason, void_codes, void_count, digest, captured_at) "
                "VALUES (?,?,?,?,?)",
                (reason, "\n".join(codes), len(codes), digest, captured_at),
            )
            snapshot_id = cur.lastrowid
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return {
        "snapshot_id": snapshot_id,
        "reason": reason,
        "void_codes": codes,
        "void_count": len(codes),
        "digest": digest,
        "captured_at": captured_at,
        "reading": "冻结",
    }


def _snapshot_dict(row) -> dict:
    codes = [c for c in row["void_codes"].split("\n") if c]
    if digest_of(codes) != row["digest"] or len(codes) != row["void_count"]:
        raise ServiceError("冻结快照明细与摘要不符，已拒绝出数", 409)
    return {
        "snapshot_id": row["id"],
        "reason": row["reason"],
        "void_codes": codes,
        "void_count": len(codes),
        "digest": row["digest"],
        "captured_at": row["captured_at"],
        "reading": "冻结",
    }


def load_snapshot(conn, snapshot_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM void_snapshots WHERE id=?", (snapshot_id,)
    ).fetchone()
    if not row:
        raise ServiceError("冻结快照不存在，请重新打开缺口对账页再生成", 409)
    return _snapshot_dict(row)


def latest_snapshot(db_path: str | None = None) -> dict | None:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM void_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return _snapshot_dict(row)


def list_snapshots(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM void_snapshots ORDER BY id DESC"
        ).fetchall()
        return [_snapshot_dict(r) for r in rows]


def void_cards(conn, codes: list[str] | None = None) -> list[dict]:
    """作废卡片：直接取作废批实物，供页面与差额数逐张对照。

    codes 给定时只出这些码的卡片，且必须仍在作废态，否则拒绝——
    这样卡片和差额数永远出自同一份作废明细。
    """
    if codes is None:
        rows = conn.execute(
            "SELECT run_code, table_name, biz_date, voided_at, void_reason, status "
            "FROM run_batches WHERE status='已作废' ORDER BY run_code"
        ).fetchall()
        return [dict(r) for r in rows]
    if not codes:
        return []
    ph = ",".join("?" for _ in codes)
    rows = conn.execute(
        "SELECT run_code, table_name, biz_date, voided_at, void_reason, status "
        f"FROM run_batches WHERE run_code IN ({ph}) ORDER BY run_code",
        codes,
    ).fetchall()
    found = {r["run_code"]: dict(r) for r in rows}
    problems = []
    for code in codes:
        card = found.get(code)
        if card is None:
            problems.append(f"{code} 在库里找不到")
        elif card["status"] != "已作废":
            problems.append(f"{code} 已不再作废")
    if problems:
        raise ServiceError("作废卡片与差额数不同源：" + "；".join(problems), 409)
    return [found[c] for c in sorted(codes)]


def cards_for_report(conn, report) -> list[dict]:
    """按一份对账单落库的作废明细出卡片，读数与差额数同源。"""
    codes = [c for c in (report["void_codes"] or "").split("\n") if c]
    return void_cards(conn, codes)


def cards_for_codes(codes: list[str], db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        return void_cards(conn, codes)


def report_void_cards(report_id: int, db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM gap_reports WHERE id=?", (report_id,)
        ).fetchone()
        if not row:
            raise ServiceError("对账单不存在", 404)
        return cards_for_report(conn, row)
