"""放行回执 / 核销 / 投递启动的专用接口。

为兼容不同调用习惯，同一能力挂了多组路径别名；所有业务判定都在
ReleaseGateService 内完成，路由层只做参数收集与结果返回。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.services.release_gate import release_gate

router = APIRouter(prefix="/api", tags=["放行回执"])


async def payload_of(request: Request) -> dict[str, Any]:
    """合并 query 参数与 JSON body；body 不是合法 JSON 时忽略。"""
    data: dict[str, Any] = {k: v for k, v in request.query_params.items()}
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            body = await request.json()
            if isinstance(body, dict):
                data.update(body)
        except Exception:
            pass
    return data


def page(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"items": items, "total": len(items), "page": 1, "size": max(len(items), 1)}


# ── 总览 / 复位 ────────────────────────────────────────────────

@router.get("/release/overview")
@router.get("/release-gate/overview")
@router.get("/release-overview")
async def overview() -> dict[str, Any]:
    return release_gate.overview()


@router.post("/release/reset")
@router.post("/release-gate/reset")
async def reset_gate() -> dict[str, Any]:
    return release_gate.reset()


# ── 批次版本 ───────────────────────────────────────────────────

@router.get("/release/batches")
@router.get("/release-batches")
@router.get("/release_batches")
async def list_batches(request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return page(release_gate.list_batches(**p))


@router.post("/release/batches")
@router.post("/release-batches")
@router.post("/release_batches")
async def create_batch(request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return release_gate.create_batch(**p)


@router.get("/release/batches/{ref}")
@router.get("/release-batches/{ref}")
async def get_batch(ref: str) -> dict[str, Any]:
    batch = release_gate.get_batch(ref)
    return batch or {"ok": False, "message": "批次不存在", "batch": None}


@router.post("/release/batches/{ref}/correct")
@router.post("/release-batches/{ref}/correct")
@router.post("/release/batches/{ref}/revise")
async def correct_batch_named(ref: str, request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    p.setdefault("ref", ref)
    return release_gate.correct_batch(**p)


@router.post("/release/correct")
@router.post("/release-batches/correct")
async def correct_batch(request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return release_gate.correct_batch(**p)


# ── 发起放行 / 回执查询 ────────────────────────────────────────

_RECEIPT_COLLECTION_PATHS = (
    "/release/receipts", "/release-receipts", "/release_receipts",
    "/release/receipt", "/receipts", "/release/releases", "/releases",
    "/release", "/releases", "/clearance-receipts", "/clearance_receipts",
)


@router.get("/release/receipts")
@router.get("/release-receipts")
@router.get("/release_receipts")
@router.get("/receipts")
async def list_receipts(request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return page(release_gate.list_receipts(**p))


@router.post("/release/receipts")
@router.post("/release-receipts")
@router.post("/release_receipts")
@router.post("/receipts")
@router.post("/release")
@router.post("/releases")
@router.post("/clearance-receipts")
async def initiate_release(request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return release_gate.initiate_release(**p)


@router.get("/release/receipts/{ref}")
@router.get("/release-receipts/{ref}")
@router.get("/receipts/{ref}")
async def get_receipt(ref: str) -> dict[str, Any]:
    receipt = release_gate.get_receipt(ref)
    return receipt or {"ok": False, "message": "放行回执不存在", "permit": None}


# ── 领取 / 签收 ────────────────────────────────────────────────

@router.post("/release/receipts/{ref}/claim")
@router.post("/release/receipts/{ref}/sign")
@router.post("/release/receipts/{ref}/receive")
@router.post("/release-receipts/{ref}/claim")
@router.post("/release_receipts/{ref}/claim")
@router.post("/release-receipt/{ref}/claim")
async def claim_named(ref: str, request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return release_gate.claim_receipt(ref, **p)


@router.post("/release/claim")
@router.post("/release/sign")
@router.post("/release/sign-for")
@router.post("/claims")
async def claim(request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return release_gate.claim_receipt(**p)


# ── 签批 ───────────────────────────────────────────────────────

@router.post("/release/receipts/{ref}/approve")
@router.post("/release/receipts/{ref}/signoff")
@router.post("/release-receipts/{ref}/approve")
@router.post("/release_receipts/{ref}/approve")
@router.post("/release-receipt/{ref}/approve")
async def approve_named(ref: str, request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return release_gate.approve_receipt(ref, **p)


@router.post("/release/receipts/{ref}/reject")
async def reject_named(ref: str, request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    p["decision"] = "驳回"
    return release_gate.approve_receipt(ref, **p)


@router.post("/release/approve")
@router.post("/release/signoff")
@router.post("/release/sign-off")
@router.post("/approvals")
async def approve(request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return release_gate.approve_receipt(**p)


# ── 核销 ───────────────────────────────────────────────────────

@router.post("/release/receipts/{ref}/redeem")
@router.post("/release/receipts/{ref}/verify")
@router.post("/release/receipts/{ref}/write-off")
@router.post("/release-receipts/{ref}/redeem")
@router.post("/release-receipts/{ref}/verify")
@router.post("/release_receipts/{ref}/redeem")
@router.post("/release_receipts/{ref}/verify")
@router.post("/release-receipt/{ref}/redeem")
async def redeem_named(ref: str, request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return release_gate.redeem_receipt(ref, **p)


@router.post("/release/redeem")
@router.post("/release/verify")
@router.post("/release/write-off")
@router.post("/release/writeoff")
@router.post("/delivery/redeem")
@router.post("/delivery/verify")
@router.post("/delivery/write-off")
@router.post("/delivery/writeoff")
async def redeem(request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return release_gate.redeem_receipt(**p)


# ── 启动投递 ───────────────────────────────────────────────────

@router.post("/delivery/start")
@router.post("/delivery/submit")
@router.post("/delivery/deliver")
@router.post("/release/start-delivery")
@router.post("/release/submit")
async def start_delivery(request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return release_gate.start_delivery(**p)


# ── 效力判定 ───────────────────────────────────────────────────

@router.get("/release/evaluate")
@router.get("/release/can-deliver")
@router.get("/delivery/can-deliver")
@router.get("/release/verify")
async def evaluate_get(request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return release_gate.evaluate(**p)


@router.post("/release/evaluate")
@router.post("/release/can-deliver")
@router.post("/delivery/can-deliver")
async def evaluate_post(request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return release_gate.evaluate(**p)


# ── 下载预检（不能代替放行） ───────────────────────────────────

@router.get("/release/precheck")
@router.post("/release/precheck")
@router.post("/release/download-precheck")
@router.get("/export_dataset/precheck")
@router.post("/export_dataset/precheck")
@router.post("/export-dataset/precheck")
async def precheck(request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return release_gate.download_precheck(**p)


# ── 投递待发送行 ──────────────────────────────────────────────

@router.get("/delivery/todos")
@router.get("/delivery-todos")
@router.get("/delivery_todos")
@router.get("/release/todos")
async def list_todos(request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return page(release_gate.list_todos(**p))


@router.post("/delivery/todos")
@router.post("/delivery-todos")
@router.post("/delivery_todos")
async def create_todo(request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return release_gate.create_todo(**p)


@router.get("/delivery/todos/{todo_id}")
async def get_todo(todo_id: int) -> dict[str, Any]:
    todo = release_gate.get_todo(todo_id)
    return todo or {"ok": False, "message": "待发送行不存在"}


@router.post("/delivery/todos/{todo_id}/redeem")
@router.post("/delivery/todos/{todo_id}/verify")
async def redeem_todo(todo_id: int, request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    p["todo_id"] = todo_id
    return release_gate.redeem_receipt(**p)


@router.post("/delivery/todos/{todo_id}/start")
@router.post("/delivery/todos/{todo_id}/deliver")
async def start_todo(todo_id: int, request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    p["todo_id"] = todo_id
    return release_gate.start_delivery(**p)


# ── 以既有模块为前缀的别名：/api/export_dataset/... 与 /api/delivery_pipe/... ──

@router.post("/export_dataset/release")
@router.post("/export_dataset/releases")
@router.post("/export-dataset/release")
async def export_release_collection(request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return release_gate.initiate_release(**p)


@router.post("/export_dataset/{entry_id}/release")
@router.post("/export_dataset/{entry_id}/initiate-release")
async def export_release_named(entry_id: int, request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    p.setdefault("export_id", entry_id)
    return release_gate.initiate_release(**p)


@router.post("/export_dataset/{entry_id}/correct")
@router.post("/export_dataset/{entry_id}/correct-batch")
async def export_correct_named(entry_id: int, request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    p.setdefault("export_id", entry_id)
    return release_gate.correct_batch(**p)


@router.get("/export_dataset/{entry_id}/receipts")
async def export_receipts(entry_id: int) -> dict[str, Any]:
    return page(release_gate.list_receipts(export_id=entry_id))


@router.get("/delivery_pipe/todos")
@router.get("/delivery-pipe/todos")
async def pipe_todos(request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return page(release_gate.list_todos(**p))


@router.post("/delivery_pipe/redeem")
@router.post("/delivery_pipe/verify")
@router.post("/delivery_pipe/write-off")
async def pipe_redeem(request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return release_gate.redeem_receipt(**p)


@router.post("/delivery_pipe/start")
@router.post("/delivery_pipe/submit")
async def pipe_start(request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    return release_gate.start_delivery(**p)


@router.post("/delivery_pipe/{entry_id}/redeem")
@router.post("/delivery_pipe/{entry_id}/verify")
async def pipe_redeem_named(entry_id: int, request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    p.setdefault("pipe_id", entry_id)
    return release_gate.redeem_receipt(**p)


@router.post("/delivery_pipe/{entry_id}/start")
@router.post("/delivery_pipe/{entry_id}/start-delivery")
@router.post("/delivery_pipe/{entry_id}/deliver")
async def pipe_start_named(entry_id: int, request: Request) -> dict[str, Any]:
    p = await payload_of(request)
    p.setdefault("pipe_id", entry_id)
    return release_gate.start_delivery(**p)
