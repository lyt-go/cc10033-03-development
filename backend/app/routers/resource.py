"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for
from app.services.release_gate import release_gate

router = APIRouter(prefix="/api", tags=["业务资源"])

# 这些模块 key 直接对应放行域聚合，路径里的 id 就是回执/待办身份
_GATE_MODULE_KEYS = {
    "release_gate", "release_receipt", "release_receipts", "release", "releases",
    "clearance_receipt", "delivery_todo", "delivery_todos",
}


def _gate_operation(action: str) -> str | None:
    """把中英文动作名归一到放行域操作；用包含匹配兼容「确认核销」等写法。"""
    text = action.strip()
    low = text.casefold()
    if any(k in text for k in ("核销",)) or low in {"redeem", "verify", "writeoff", "write-off", "check-receipt"}:
        return "redeem"
    if any(k in text for k in ("领取", "签收", "签领")) or low in {"claim", "sign", "sign-for", "receive"}:
        return "claim"
    if "驳回" in text or "拒绝" in text or low in {"reject"}:
        return "reject"
    if "签批" in text or "批准" in text or "同意" in text or low in {"approve", "signoff", "sign-off"}:
        return "approve"
    if "更正" in text or "修订版本" in text or low in {"correct", "revise"}:
        return "correct"
    if "发起放行" in text or text == "放行" or low in {"release", "issue-release"}:
        return "release"
    if "启动投递" in text or "开始投递" in text or low in {"start-delivery", "submit-delivery", "deliver-batch"}:
        return "start"
    if "预检" in text or low in {"precheck", "download-precheck"}:
        return "precheck"
    if low in {"can-deliver", "evaluate", "check-permit"}:
        return "evaluate"
    return None


def _run_gate_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult | None:
    operation = _gate_operation(payload.action)
    if operation is None and module not in _GATE_MODULE_KEYS:
        return None
    if operation is None:
        # 放行域模块上的未知动作，交回基类逻辑产生标准报错
        return None

    kwargs = payload.model_dump(exclude_none=True)
    kwargs.pop("action", None)
    kwargs.pop("remark", None)
    # 兼容把附加字段再包一层 values/payload/data 的调用方式
    for nested_key in ("values", "payload", "data"):
        nested = kwargs.pop(nested_key, None)
        if isinstance(nested, dict):
            for key, value in nested.items():
                kwargs.setdefault(key, value)
    # pydantic 已显式声明的字段也在 dump 中；extra 键（如「接收方」）同样保留

    if module == "export_dataset":
        kwargs.setdefault("export_id", entry_id)
    elif module == "delivery_pipe":
        kwargs.setdefault("pipe_id", entry_id)
    elif module in ("delivery_todo", "delivery_todos"):
        kwargs.setdefault("todo_id", entry_id)
        kwargs.setdefault("todo_id", entry_id)
    elif module in ("release_receipt", "release_receipts", "release", "releases", "clearance_receipt"):
        kwargs.setdefault("permit_id", entry_id)

    # 待办行核销/启动：若给的是 todo_id，由服务自行补 permit
    op = operation
    if op == "release":
        verdict = release_gate.initiate_release(**kwargs)
    elif op == "correct":
        verdict = release_gate.correct_batch(**kwargs)
    elif op == "claim":
        verdict = release_gate.claim_receipt(**kwargs)
    elif op == "approve":
        kwargs.setdefault("decision", "通过")
        verdict = release_gate.approve_receipt(**kwargs)
    elif op == "reject":
        kwargs["decision"] = "驳回"
        verdict = release_gate.approve_receipt(**kwargs)
    elif op == "redeem":
        verdict = release_gate.redeem_receipt(**kwargs)
    elif op == "start":
        verdict = release_gate.start_delivery(**kwargs)
    elif op == "precheck":
        verdict = release_gate.download_precheck(**kwargs)
    else:
        verdict = release_gate.evaluate(**kwargs)

    ok = bool(verdict.get("ok", verdict.get("allowed", False)))
    return ActionResult(
        ok=ok,
        message=str(verdict.get("message", "")),
        entry=verdict,
        verdict=verdict,
        permit=verdict.get("permit"),
    )


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    # 兼容两种 body：{values: {...}} 与直接平铺字段
    values = dict(payload.values or {})
    extra = getattr(payload, "model_extra", None) or {}
    for key, value in extra.items():
        if key != "remark" and key not in values:
            values[key] = value
    entry, missing = service.create_entry(values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    # 放行/核销/签收/更正等跨模块动作优先走放行域状态机
    gate_result = _run_gate_action(module, entry_id, payload)
    if gate_result is not None:
        return gate_result
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)
