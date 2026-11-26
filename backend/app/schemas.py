"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: bool
    message: str
    entry: dict[str, Any] | None = None
    verdict: dict[str, Any] | None = None
    permit: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    # 允许直接把字段放在顶层（不必包一层 values），路由会合并进 values。
    model_config = ConfigDict(extra="allow")

    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    # 放行/核销等动作需要 receiver/permit_id/request_id 等附加字段，
    # 允许并保留任意额外键，避免不同调用方的字段命名被丢弃。
    model_config = ConfigDict(extra="allow")

    action: str
    remark: str | None = None
    receiver: str | None = None
    permit_id: str | int | None = None
    receipt_id: str | int | None = None
    request_id: str | None = None
    idempotency_key: str | None = None
    decision: str | None = None
    operator: str | None = None
    owner: str | None = None
    revision: str | int | None = None
    batch_no: str | None = None
    export_id: int | str | None = None
    todo_id: int | str | None = None
    pipe_id: int | str | None = None
