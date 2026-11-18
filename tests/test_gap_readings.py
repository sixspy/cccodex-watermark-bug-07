import asyncio

import pytest

from app.db import get_db


async def _void_one(authed, batch_id, reason="作废验证"):
    r = await authed.post(f"/api/batches/{batch_id}/void", json={"reason": reason})
    assert r.status == 200
    return (await r.json())["batch"]


async def _pending(authed):
    items = (await (await authed.get("/api/batches")).json())["items"]
    return [b for b in items if b["status"] == "待盖印"]


@pytest.mark.asyncio
async def test_frozen_and_rescan_readings_differ(authed):
    pending = await _pending(authed)
    assert len(pending) >= 2

    ctx = (await (await authed.get("/api/gap-reports/context")).json())
    snap_id = ctx["snapshot"]["snapshot_id"]
    assert ctx["snapshot"]["void_count"] == 0

    # 打开页冻结之后再作废一批：冻结读数看不到它，重扫读数看得到。
    await _void_one(authed, pending[0]["id"])

    frozen = (
        await (
            await authed.post(
                "/api/gap-reports",
                json={
                    "as_of_date": "2026-11-16",
                    "reading": "冻结",
                    "snapshot_id": snap_id,
                },
            )
        ).json()
    )["report"]
    rescan = (
        await (
            await authed.post(
                "/api/gap-reports",
                json={"as_of_date": "2026-11-16", "reading": "重扫"},
            )
        ).json()
    )["report"]

    assert frozen["reading"] == "冻结"
    assert frozen["void_count"] == 0
    assert rescan["reading"] == "重扫"
    assert rescan["void_count"] == 1
    assert rescan["void_codes"] == [pending[0]["run_code"]]
    # 作废批进差额 = 应盖印少一个，缺口少一个
    assert frozen["expected_count"] == rescan["expected_count"] + 1
    assert frozen["gap_count"] == rescan["gap_count"] + 1
    assert rescan["seq"] > frozen["seq"]

    # 同一冻结读数的二次出数必须和第一次同数（读数钉死）
    frozen2 = (
        await (
            await authed.post(
                "/api/gap-reports",
                json={
                    "as_of_date": "2026-11-16",
                    "reading": "冻结",
                    "snapshot_id": snap_id,
                },
            )
        ).json()
    )["report"]
    assert frozen2["gap_count"] == frozen["gap_count"]
    assert frozen2["void_codes"] == frozen["void_codes"]
    assert frozen2["seq"] != frozen["seq"]


@pytest.mark.asyncio
async def test_reading_must_be_one_of_two(authed):
    r = await authed.post(
        "/api/gap-reports", json={"as_of_date": "2026-11-16", "reading": "随便"}
    )
    assert r.status == 400
    assert "冻结" in (await r.json())["error"]


@pytest.mark.asyncio
async def test_frozen_requires_snapshot(authed):
    r = await authed.post(
        "/api/gap-reports", json={"as_of_date": "2026-11-16", "reading": "冻结"}
    )
    assert r.status == 409
    assert "快照" in (await r.json())["error"]


@pytest.mark.asyncio
async def test_void_cards_same_source_as_report(authed):
    pending = await _pending(authed)
    batch = await _void_one(authed, pending[0]["id"], "同源核对")
    report = (
        await (
            await authed.post(
                "/api/gap-reports",
                json={"as_of_date": "2026-11-16", "reading": "重扫"},
            )
        ).json()
    )["report"]

    cards = (
        await (await authed.get(f"/api/gap-reports/{report['id']}/void-cards")).json()
    )["void_cards"]
    assert [c["run_code"] for c in cards] == report["void_codes"]
    assert cards[0]["void_reason"] == "同源核对"
    assert cards[0]["voided_at"]
    assert cards[0]["run_code"] == batch["run_code"]

    ok = await authed.post(f"/api/gap-reports/{report['id']}/reconcile", json={})
    assert ok.status == 200
    assert (await ok.json())["recount"]["void_in_day"] == report["void_count"]


@pytest.mark.asyncio
async def test_drifted_report_is_rejected(authed, monkeypatch):
    pending = await _pending(authed)
    await _void_one(authed, pending[0]["id"])
    report = (
        await (
            await authed.post(
                "/api/gap-reports",
                json={"as_of_date": "2026-11-16", "reading": "重扫"},
            )
        ).json()
    )["report"]

    # 把摘要改坏（模拟“只改计数/措辞”），核对必须拒
    from app.config import Config

    with get_db(Config.database()) as conn:
        conn.execute(
            "UPDATE gap_reports SET digest='bad' WHERE id=?", (report["id"],)
        )
    r = await authed.post(f"/api/gap-reports/{report['id']}/reconcile", json={})
    assert r.status == 409
    assert "摘要" in (await r.json())["error"]

    # 明细与计数不符也必须拒
    with get_db(Config.database()) as conn:
        conn.execute(
            "UPDATE gap_reports SET digest=?, void_count=void_count+1 WHERE id=?",
            (report["digest"], report["id"]),
        )
    r = await authed.post(f"/api/gap-reports/{report['id']}/reconcile", json={})
    assert r.status == 409
    assert "计数" in (await r.json())["error"]


@pytest.mark.asyncio
async def test_void_card_drift_is_rejected(authed):
    pending = await _pending(authed)
    await _void_one(authed, pending[0]["id"])
    report = (
        await (
            await authed.post(
                "/api/gap-reports",
                json={"as_of_date": "2026-11-16", "reading": "重扫"},
            )
        ).json()
    )["report"]

    # 作废批被改回非作废态：卡片刻不出来，必须拒
    from app.config import Config

    with get_db(Config.database()) as conn:
        conn.execute(
            "UPDATE run_batches SET status='待盖印' WHERE run_code=?",
            (report["void_codes"][0],),
        )
    r = await authed.get(f"/api/gap-reports/{report['id']}/void-cards")
    assert r.status == 409
    assert "不同源" in (await r.json())["error"]


@pytest.mark.asyncio
async def test_verify_two_rounds_align_and_progress_marked(authed):
    pending = await _pending(authed)
    await _void_one(authed, pending[0]["id"])
    report = (
        await (
            await authed.post(
                "/api/gap-reports",
                json={"as_of_date": "2026-11-16", "reading": "重扫"},
            )
        ).json()
    )["report"]

    v = (
        await (
            await authed.post(f"/api/gap-reports/{report['id']}/verify", json={})
        ).json()
    )["verification"]
    assert v["round1_gap"] == v["round2_gap"] == report["gap_count"]
    assert v["aligned"] == 1
    assert v["reading"] == "重扫"

    items = (await (await authed.get("/api/verifications")).json())["items"]
    assert items[0]["report_id"] == report["id"]
    assert items[0]["aligned"] == 1

    # 编排任务入库可查
    tasks = (await (await authed.get("/api/orchestration-tasks")).json())["items"]
    kinds = [t["kind"] for t in tasks]
    assert "缺口对账出数" in kinds
    assert "缺口复验" in kinds
    gap_task = next(t for t in tasks if t["kind"] == "缺口对账出数")
    assert gap_task["report_id"] == report["id"]
    assert gap_task["reading"] == "重扫"
    assert gap_task["seq"] == report["seq"]


@pytest.mark.asyncio
async def test_frozen_report_survives_later_voids(authed):
    """冻结读数钉死的是打开页那一刻的作废集合：之后再作废，旧数仍自洽。"""
    ctx = (await (await authed.get("/api/gap-reports/context")).json())
    snap_id = ctx["snapshot"]["snapshot_id"]
    frozen = (
        await (
            await authed.post(
                "/api/gap-reports",
                json={
                    "as_of_date": "2026-11-16",
                    "reading": "冻结",
                    "snapshot_id": snap_id,
                },
            )
        ).json()
    )["report"]

    pending = await _pending(authed)
    await _void_one(authed, pending[0]["id"])

    ok = await authed.post(f"/api/gap-reports/{frozen['id']}/reconcile", json={})
    assert ok.status == 200, await ok.text()
    recount = (await ok.json())["recount"]
    assert recount["void_in_day"] == frozen["void_count"]

    v = (
        await (
            await authed.post(f"/api/gap-reports/{frozen['id']}/verify", json={})
        ).json()
    )["verification"]
    assert v["aligned"] == 1
    assert v["reading"] == "冻结"


@pytest.mark.asyncio
async def test_frozen_snapshot_rejects_unvoided_batch(authed):
    """快照里记的作废批后来不作废了：冻结出数必须拒，不能悄悄按旧数走。"""
    from app.config import Config

    pending = await _pending(authed)
    await _void_one(authed, pending[0]["id"])
    ctx = (await (await authed.get("/api/gap-reports/context")).json())
    snap_id = ctx["snapshot"]["snapshot_id"]
    assert pending[0]["run_code"] in ctx["snapshot"]["void_codes"]

    with get_db(Config.database()) as conn:
        conn.execute(
            "UPDATE run_batches SET status='待盖印' WHERE run_code=?",
            (pending[0]["run_code"],),
        )

    r = await authed.post(
        "/api/gap-reports",
        json={"as_of_date": "2026-11-16", "reading": "冻结", "snapshot_id": snap_id},
    )
    assert r.status == 409
    assert "不再作废" in (await r.json())["error"]


@pytest.mark.asyncio
async def test_later_stamp_does_not_invalidate_old_report(authed):
    """读数只管作废集合；事后又盖印，旧对账单仍应核对通过。"""
    report = (
        await (
            await authed.post(
                "/api/gap-reports",
                json={"as_of_date": "2026-11-16", "reading": "重扫"},
            )
        ).json()
    )["report"]
    pending = await _pending(authed)
    stamp = await authed.post(
        f"/api/batches/{pending[0]['id']}/stamp",
        json={"watermark_value": "WM-LATER-1"},
    )
    assert stamp.status == 200

    ok = await authed.post(f"/api/gap-reports/{report['id']}/reconcile", json={})
    assert ok.status == 200, await ok.text()
    recount = (await ok.json())["recount"]
    assert recount["stamped"] == report["stamped_count"]
    assert recount["gap"] == report["gap_count"]

    v = (
        await (
            await authed.post(f"/api/gap-reports/{report['id']}/verify", json={})
        ).json()
    )["verification"]
    assert v["aligned"] == 1


@pytest.mark.asyncio
async def test_concurrent_rescan_gets_distinct_seq(authed):
    """两边同时抢重扫：序号不撞、摘要不互相盖掉。"""
    pending = await _pending(authed)
    await _void_one(authed, pending[0]["id"])

    async def fire():
        r = await authed.post(
            "/api/gap-reports",
            json={"as_of_date": "2026-11-16", "reading": "重扫"},
        )
        assert r.status == 200, await r.text()
        return (await r.json())["report"]

    a, b = await asyncio.gather(fire(), fire())
    assert a["id"] != b["id"]
    assert a["seq"] != b["seq"]
    reports = (await (await authed.get("/api/gap-reports")).json())["items"]
    seqs = [r["seq"] for r in reports]
    assert len(seqs) == len(set(seqs)), seqs
    # 两份摘要各自带自己的序号，没有被覆盖
    assert f"序 {a['seq']}" in next(r["summary"] for r in reports if r["id"] == a["id"])
    assert f"序 {b['seq']}" in next(r["summary"] for r in reports if r["id"] == b["id"])
