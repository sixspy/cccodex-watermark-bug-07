from pathlib import Path

from blacksheep import Application, Content, Cookie, Request, Response
from blacksheep.server.responses import redirect
from itsdangerous import BadSignature, URLSafeSerializer

from app.config import Config
from app.db import init_db
from app.services import (
    capture_snapshot,
    cards_for_codes,
    get_batch,
    list_batches,
    list_reports,
    list_tasks,
    list_templates,
    list_verifications,
    reconcile_report,
    report_void_cards,
    run_gap_report,
    stamp_batch,
    trace_downstream,
    verify_report,
    void_batch,
)
from app.services.errors import ServiceError

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
serializer = URLSafeSerializer(Config.SECRET, salt="cc-watermark-session")

app = Application()
app.serve_files(str(STATIC), root_path="/static")


@app.on_start
async def _on_start(_app) -> None:
    init_db()


def _read_session(request: Request) -> dict:
    raw = request.cookies.get("watermark_session")
    if not raw:
        return {}
    try:
        data = serializer.loads(raw)
        return data if isinstance(data, dict) else {}
    except BadSignature:
        return {}


def require_user(request: Request) -> str:
    user = _read_session(request).get("user")
    if user != Config.ADMIN_USER:
        raise ServiceError("未登录", 401)
    return user


def json_response(data: dict, status: int = 200) -> Response:
    import json

    return Response(
        status,
        content=Content(
            b"application/json",
            json.dumps(data, ensure_ascii=False).encode("utf-8"),
        ),
    )


def html_response(path: Path) -> Response:
    return Response(
        200,
        content=Content(b"text/html; charset=utf-8", path.read_bytes()),
    )


@app.router.get("/")
async def root(request: Request):
    if _read_session(request).get("user") == Config.ADMIN_USER:
        return redirect("/app/")
    return redirect("/login")


@app.router.get("/login")
async def login_page():
    return html_response(STATIC / "login.html")


@app.router.get("/app/")
async def spa_page(request: Request):
    if _read_session(request).get("user") != Config.ADMIN_USER:
        return redirect("/login")
    return html_response(STATIC / "index.html")


@app.router.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    user = str(body.get("username", "")).strip()
    pwd = str(body.get("password", ""))
    if user != Config.ADMIN_USER or pwd != Config.ADMIN_PASSWORD:
        return json_response({"ok": False, "error": "账号或密码错误"}, 400)
    resp = json_response({"ok": True, "user": user})
    resp.set_cookie(
        Cookie(
            "watermark_session",
            serializer.dumps({"user": user}),
            http_only=True,
            path="/",
        )
    )
    return resp


@app.router.post("/api/logout")
async def api_logout():
    resp = json_response({"ok": True})
    resp.unset_cookie("watermark_session")
    return resp


@app.router.get("/api/meta")
async def api_meta(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response(
        {
            "lab_name": Config.LAB_NAME,
            "product_line": "cc-watermark-lab",
            "chain": ["水印模板", "批次盖印", "下游追索", "缺口对账"],
        }
    )


@app.router.get("/api/templates")
async def api_templates(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    status = request.query.get("status")
    status_val = status[0] if status else None
    return json_response({"items": list_templates(status=status_val)})


@app.router.get("/api/batches")
async def api_batches(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    status = request.query.get("status")
    status_val = status[0] if status else None
    return json_response({"items": list_batches(status=status_val)})


@app.router.get("/api/batches/{batch_id}")
async def api_batch_detail(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    batch_id = int(request.route_values["batch_id"])
    batch = get_batch(batch_id)
    if not batch:
        return json_response({"error": "批次不存在"}, 404)
    return json_response({"batch": batch})


@app.router.post("/api/batches/{batch_id}/stamp")
async def api_stamp(request: Request):
    try:
        require_user(request)
        batch_id = int(request.route_values["batch_id"])
        body = await request.json()
        batch = stamp_batch(batch_id, str(body.get("watermark_value", "")))
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, "batch": batch})


@app.router.post("/api/batches/{batch_id}/void")
async def api_void(request: Request):
    try:
        require_user(request)
        batch_id = int(request.route_values["batch_id"])
        body = await request.json()
        batch = void_batch(batch_id, str(body.get("reason", "")))
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, "batch": batch})


@app.router.post("/api/trace")
async def api_trace(request: Request):
    try:
        require_user(request)
        body = await request.json()
        result = trace_downstream(str(body.get("watermark_value", "")))
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, **result})


@app.router.get("/api/gap-reports")
async def api_gap_list(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response({"items": list_reports()})


@app.router.get("/api/gap-reports/context")
async def api_gap_context(request: Request):
    """打开缺口对账页时调用：冻结一份作废集合快照，并给出卡片与已出过的数。"""
    try:
        require_user(request)
        snap = capture_snapshot()
        items = list_reports()
        cards = cards_for_codes(snap["void_codes"])
        latest = items[0] if items else None
        drift = None
        if latest is not None:
            drift = sorted(set(latest["void_codes"]) ^ set(snap["void_codes"]))
        return json_response(
            {
                "ok": True,
                "snapshot": snap,
                "void_cards": cards,
                "latest_report": latest,
                "drift": drift,
            }
        )
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)


@app.router.get("/api/gap-reports/{report_id}/void-cards")
async def api_void_cards(request: Request):
    try:
        require_user(request)
        report_id = int(request.route_values["report_id"])
        cards = report_void_cards(report_id)
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, "void_cards": cards})


@app.router.post("/api/gap-reports")
async def api_gap_run(request: Request):
    try:
        require_user(request)
        body = await request.json()
        reading = str(body.get("reading", "重扫"))
        snapshot_id = body.get("snapshot_id")
        report = run_gap_report(
            str(body.get("as_of_date", "2026-11-16")),
            reading=reading,
            snapshot_id=int(snapshot_id) if snapshot_id is not None else None,
        )
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, "report": report})


@app.router.post("/api/gap-reports/{report_id}/reconcile")
async def api_gap_reconcile(request: Request):
    try:
        require_user(request)
        report_id = int(request.route_values["report_id"])
        result = reconcile_report(report_id)
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({**result})


@app.router.post("/api/gap-reports/{report_id}/verify")
async def api_gap_verify(request: Request):
    try:
        require_user(request)
        report_id = int(request.route_values["report_id"])
        result = verify_report(report_id)
    except ServiceError as exc:
        return json_response({"ok": False, "error": exc.message}, exc.status_code)
    return json_response({"ok": True, "verification": result})


@app.router.get("/api/verifications")
async def api_verifications(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response({"items": list_verifications()})


@app.router.get("/api/orchestration-tasks")
async def api_tasks(request: Request):
    try:
        require_user(request)
    except ServiceError as exc:
        return json_response({"error": exc.message}, exc.status_code)
    return json_response({"items": list_tasks()})
