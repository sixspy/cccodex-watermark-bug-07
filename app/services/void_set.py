from app.db import get_db


def void_snapshot(db_path: str | None = None) -> list[str]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT run_code FROM run_batches WHERE status='已作废'"
        ).fetchall()
        return [r["run_code"] for r in rows]
