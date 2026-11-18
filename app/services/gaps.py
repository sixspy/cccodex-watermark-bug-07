from app.db import get_db
from app.services.errors import ServiceError
from app.services.void_set import (
    digest_of,
    load_snapshot,
    read_void_codes,
    void_cards,
)

# 差额出数的读数时机只有两种，二选一，且都在“一个事务内”一次落成：
#   冻结 —— 用打开页面时冻结的作废集合快照（snapshot_id）去算；
#   重扫 —— 在提交瞬间重扫库内作废态去算。
# 应盖印 = 截至该业务日的全部批次 − 该读数作废集合里落在该日及以前的批次。
# 作废明细、计数、摘要三者同源，任何一项对不上就拒绝出数。

READINGS = ("冻结", "重扫")


def _reading_value(reading: str | None) -> str:
    value = (reading or "").strip()
    if value not in READINGS:
        raise ServiceError("读数只能是“冻结”或“重扫”", 400)
    return value


def _codes_list(raw: str | None) -> list[str]:
    return [c for c in (raw or "").split("\n") if c]


def _report_dict(row) -> dict:
    item = dict(row)
    item["void_codes"] = _codes_list(item.get("void_codes"))
    return item


def list_reports(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM gap_reports ORDER BY id DESC"
        ).fetchall()
    return [_report_dict(r) for r in rows]


def _void_dates(conn, void_codes: list[str]) -> dict[str, str]:
    """作废码到业务日的映射；任何一个码在库里不再作废即视为不同源。"""
    if not void_codes:
        return {}
    ph = ",".join("?" for _ in void_codes)
    rows = conn.execute(
        f"SELECT run_code, biz_date, status FROM run_batches WHERE run_code IN ({ph})",
        void_codes,
    ).fetchall()
    found = {r["run_code"]: r for r in rows}
    missing = [c for c in void_codes if c not in found]
    if missing:
        raise ServiceError(
            "对账单记的作废批在库里找不到：" + "、".join(missing) + "；已拒绝出数", 409
        )
    changed = [c for c in void_codes if found[c]["status"] != "已作废"]
    if changed:
        raise ServiceError(
            "对账单记的作废批已不再作废：" + "、".join(changed) + "；已拒绝出数", 409
        )
    return {c: found[c]["biz_date"] for c in void_codes}


def _expected_from_void(conn, day: str, void_codes: list[str]) -> dict:
    """应盖印只由“作废集合”这一份读数决定，不掺别的状态。

    同源口径：读数里列出的作废批整批退出分母；不在读数里的批次即使库内
    已作废，仍算应盖印（缺口里能看到它）——这正是两种读数的差别。
    """
    dates = _void_dates(conn, void_codes)
    due_all = conn.execute(
        "SELECT COUNT(*) AS c FROM run_batches WHERE biz_date<=?", (day,)
    ).fetchone()["c"]
    void_in_day = sum(1 for code in void_codes if dates[code] <= day)
    return {
        "due_all": due_all,
        "void_in_day": void_in_day,
        "expected": due_all - void_in_day,
    }


def _current_stamped(conn, day: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM run_batches WHERE biz_date<=? AND status='已盖印'",
        (day,),
    ).fetchone()["c"]


def _counts(conn, day: str, void_codes: list[str]) -> dict:
    """出数时用：读数决定应盖印，已盖印取当下实物。"""
    base = _expected_from_void(conn, day, void_codes)
    stamped = _current_stamped(conn, day)
    return {**base, "stamped": stamped, "gap": base["expected"] - stamped}


def run_gap_report(
    as_of_date: str,
    db_path: str | None = None,
    reading: str = "重扫",
    snapshot_id: int | None = None,
) -> dict:
    day = (as_of_date or "").strip() or "2026-11-16"
    mode = _reading_value(reading)
    with get_db(db_path) as conn:
        # BEGIN IMMEDIATE 把取序号、读作废实物、算数、落摘要锁成一个写事务：
        # 两边同时点重扫时串行入队，后到者拿到新序号，不会互相盖掉摘要。
        conn.execute("BEGIN IMMEDIATE")
        try:
            if mode == "冻结":
                if snapshot_id is None:
                    raise ServiceError(
                        "缺少冻结快照，请重新打开缺口对账页再生成", 409
                    )
                snap = load_snapshot(conn, snapshot_id)
                void_codes = snap["void_codes"]
                source = f"打开页快照#{snap['snapshot_id']}"
            else:
                void_codes = read_void_codes(conn)
                source = "提交瞬间重扫"
            counts = _counts(conn, day, void_codes)
            digest = digest_of(void_codes)
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM gap_reports"
            ).fetchone()["s"]
            summary = (
                f"截至 {day}：应盖印 {counts['expected']}，"
                f"已盖印 {counts['stamped']}，缺口 {counts['gap']}"
                f"（作废 {len(void_codes)} 批，读数 {mode}·{source}，序 {seq}）"
            )
            cur = conn.execute(
                "INSERT INTO gap_reports("
                "as_of_date, expected_count, stamped_count, gap_count, summary, "
                "seq, reading, void_codes, void_count, digest) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    day,
                    counts["expected"],
                    counts["stamped"],
                    counts["gap"],
                    summary,
                    seq,
                    mode,
                    "\n".join(void_codes),
                    len(void_codes),
                    digest,
                ),
            )
            report_id = cur.lastrowid
            conn.execute(
                "INSERT INTO orchestration_tasks("
                "task_code, kind, report_id, reading, seq, status, detail, created_at) "
                "VALUES (?,?,?,?,?,?,?,datetime('now'))",
                (
                    f"GAP-{seq:04d}-R{report_id}",
                    "缺口对账出数",
                    report_id,
                    mode,
                    seq,
                    "已完成",
                    f"读数 {mode}（{source}）；作废 {len(void_codes)} 批；"
                    f"应盖印 {counts['expected']}，缺口 {counts['gap']}",
                ),
            )
            row = conn.execute(
                "SELECT * FROM gap_reports WHERE id=?", (report_id,)
            ).fetchone()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    item = _report_dict(row)
    item["source"] = source
    return item


def _require_traceable(report: dict) -> None:
    if not report.get("digest") or report.get("reading") not in READINGS:
        raise ServiceError(
            "这份对账单是旧版出的数，没记作废明细与读数，无法核对；"
            "请在缺口对账页按冻结或重扫重新出数",
            409,
        )


def reconcile_report(report_id: int, db_path: str | None = None) -> dict:
    """按库内实物核对一份对账单：作废卡片、明细、计数、摘要、算术全对上才放行。

    只核“读数该管的那一半”：应盖印由作废实物重算，已盖印取对账单自己
    记下的那一刻的值（之后再盖印不会把旧对账单判成错）。
    """
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM gap_reports WHERE id=?", (report_id,)
        ).fetchone()
        if not row:
            raise ServiceError("对账单不存在", 404)
        report = _report_dict(row)
        _require_traceable(report)
        codes = report["void_codes"]
        if digest_of(codes) != report["digest"]:
            raise ServiceError("对账单作废摘要与明细不符，已拒绝", 409)
        if len(codes) != report["void_count"]:
            raise ServiceError("对账单作废计数与明细不符，已拒绝", 409)
        void_cards(conn, codes)  # 卡片实物不同源时直接抛 409
        base = _expected_from_void(conn, report["as_of_date"], codes)
        stamped = report["stamped_count"]
        gap = base["expected"] - stamped
        if base["expected"] != report["expected_count"]:
            raise ServiceError(
                f"应盖印对不上：按作废实物重算 {base['expected']}，"
                f"对账单记 {report['expected_count']}；已拒绝",
                409,
            )
        if gap != report["gap_count"]:
            raise ServiceError(
                f"缺口对不上：按落库数重算 {gap}，"
                f"对账单记 {report['gap_count']}；已拒绝",
                409,
            )
    counts = {**base, "stamped": stamped, "gap": gap}
    return {"ok": True, "report_id": report_id, "recount": counts}


def _stored_gap(conn, report: dict) -> int:
    """第二轮：不碰实物，只用对账单自己落库的三个数重推一遍。"""
    return report["expected_count"] - report["stamped_count"]


def verify_report(report_id: int, db_path: str | None = None) -> dict:
    """复验两轮，两轮走不同算法，必须落到同一个缺口数。

    第一轮：按对账单的读数去实物里重算应盖印（冻结用快照、重扫用当前作废态），
            配对账单记下的已盖印；
    第二轮：只用对账单自己落库的三个数重推一遍。
    两轮若不同，说明读数或实物已漂，判未对齐并留档。
    """
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM gap_reports WHERE id=?", (report_id,)
        ).fetchone()
        if not row:
            raise ServiceError("对账单不存在", 404)
        report = _report_dict(row)
        _require_traceable(report)
        codes = report["void_codes"]
        round1 = (
            _expected_from_void(conn, report["as_of_date"], codes)["expected"]
            - report["stamped_count"]
        )
        round2 = _stored_gap(conn, report)
        aligned = round1 == round2 == report["gap_count"]
        detail = (
            f"第一轮按读数重算缺口 {round1}，第二轮按落库明细重推缺口 {round2}，"
            f"对账单缺口 {report['gap_count']}"
        )
        detail += "；两轮对齐" if aligned else "；两轮不一致，复验不通过"
        conn.execute(
            "INSERT INTO gap_verifications("
            "report_id, round1_gap, round2_gap, aligned, detail, "
            "reading, seq, void_count, checked_at) "
            "VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
            (
                report_id,
                round1,
                round2,
                1 if aligned else 0,
                detail,
                report["reading"] or "重扫",
                report["seq"],
                report["void_count"],
            ),
        )
        verify_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT INTO orchestration_tasks("
            "task_code, kind, report_id, reading, seq, status, detail, created_at) "
            "VALUES (?,?,?,?,?,?,?,datetime('now'))",
            (
                f"VER-{report_id:04d}-{verify_id}",
                "缺口复验",
                report_id,
                report["reading"] or "重扫",
                report["seq"],
                "已对齐" if aligned else "未对齐",
                detail,
            ),
        )
        out = conn.execute(
            "SELECT * FROM gap_verifications WHERE id=?", (verify_id,)
        ).fetchone()
    result = dict(out)
    result["reading"] = report["reading"] or "重扫"
    result["seq"] = report["seq"]
    result["void_count"] = report["void_count"]
    return result


def list_verifications(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM gap_verifications ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def list_tasks(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM orchestration_tasks ORDER BY id DESC"
        ).fetchall()
    return [dict(r) for r in rows]
