"""交接放行领域：导出批次版本、放行回执（许可身份）、领取/签收、签批回写与投递核销。

核心不变量（对应验收口径）：
1. 放行回执的作用域必须指向确定的「批次版本 + 接收方」。
2. 导出完成、文件生成、下载预检都不等于放行；投递启动/核销只认有效回执。
3. 更正批次产生新版本（R1 -> R2），旧版本记录与旧回执原样保留、可查证，
   但旧许可不再是「当前实际有效」的放行，不能再被核销启动新投递。
4. 新接收方不能借用旧接收方的许可（接收方必须逐字匹配）。
5. 领取（签收）按 (回执, 接收方) 与幂等键去重，重复提交原领取请求返回同一许可身份。
6. 更正与领取/核销并发时用同一把锁做判定+落库，只有当前有效放行能被核销。
7. 所有判定响应都带「许可身份」和 can_send / needs_reconfirm，
   调用方据此判断能否发送、是否还要请负责人确认。

状态事实（status）与当前效力（effective/can_send）分开：
旧回执即使因更正而失效，其 status 仍是当时的「已放行/已签收」，保证原样查证。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 回执事实状态
STATUS_RELEASED = "已放行"
STATUS_CLAIMED = "已签收"  # 领取/签收后的事实状态
STATUS_REDEEMED = "已核销"
STATUS_REJECTED = "已驳回"

# 投递待办状态
TODO_PENDING = "待发送"
TODO_REDEEMED = "已核销"
TODO_DELIVERING = "投递中"
TODO_DELIVERED = "已发送"

CURRENT = "current"
CORRECTED = "corrected"

_LOCK = threading.RLock()

# 允许调用方用各种键名传同一个概念
_RECEIVER_KEYS = ("receiver", "receiver_id", "receiver_name", "recipient", "recipient_name",
                  "target_receiver", "destination", "target",
                  "接收方", "接收方名称", "接收单位", "客户", "收货方")
_BATCH_KEYS = ("batch_key", "batch_no", "batch", "batch_id", "data_batch",
               "批次号", "数据批次", "批次", "批次编号", "批号")
_EXPORT_KEYS = ("export_id", "entry_id", "export_entry_id", "task_id", "export_task_id",
                "导出任务", "导出任务id", "任务id")
_REVISION_KEYS = ("revision", "rev", "version", "revision_label", "version_label", "版本", "版本号")
_PERMIT_KEYS = ("permit_id", "permit_no", "permit", "receipt_id", "receipt_no", "receipt_code",
                "receipt", "release_id", "release_no", "pass_id", "pass_no", "license_id",
                "clearance_id", "credential", "token", "code",
                "回执编号", "回执id", "许可身份", "放行回执", "回执", "许可证号")
_IDEM_KEYS = ("idempotency_key", "request_id", "client_request_id", "idem_key",
              "submission_id", "请求编号", "幂等键")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(r.get("id", 0)) for r in rows), default=0) + 1


def _first(payload: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return default


def _revision_of(value: Any) -> int | None:
    """把 2 / '2' / 'R2' / 'r2' / 'v2' 解析成整数版本号。"""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    for prefix in ("R", "r", "V", "v", "版本"):
        if text.startswith(prefix):
            tail = text[len(prefix):].strip()
            if tail.isdigit():
                return int(tail)
    digits = "".join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


class ReleaseGateService(BaseModuleService):
    """放行回执聚合服务；无实例状态，全部状态落在全局 store 的独立表中。"""

    MODULE = "release_receipts"
    ENTITY = "放行回执"
    LIST_FIELDS = ["permit_id", "scope", "receiver", "revision_label", "status"]
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER = [STATUS_RELEASED, STATUS_CLAIMED, STATUS_REDEEMED]

    def __init__(self) -> None:
        # 兼容可能的 BaseModuleService 初始化习惯
        pass

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """兼容通用 POST /api/release_receipts：直接按「发起放行」处理。"""
        verdict = self.initiate_release(**(values or {}))
        if not verdict.get("ok"):
            missing = []
            if verdict.get("code") == "RECEIVER_REQUIRED":
                missing.append("接收方")
            if verdict.get("code") == "BATCH_REQUIRED":
                missing.append("批次")
            return None, missing or [verdict.get("message", "放行失败")]
        return verdict.get("permit"), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:  # type: ignore[override]
        """通用动作兜底：/api/release_receipts/{id}/actions 上的中文动作。"""
        ref = {"receipt_id": entry_id}
        mapping = {
            "领取": "claim", "签收": "claim", "claim": "claim",
            "签批通过": "approve", "同意": "approve", "approve": "approve",
            "签批驳回": "reject", "驳回": "reject",
            "核销": "redeem", "redeem": "redeem",
        }
        kind = mapping.get(action)
        if kind == "claim":
            v = self.claim_receipt(permit_id=entry_id)
        elif kind == "approve":
            v = self.approve_receipt(permit_id=entry_id, decision="通过")
        elif kind == "reject":
            v = self.approve_receipt(permit_id=entry_id, decision="驳回")
        elif kind == "redeem":
            v = self.redeem_receipt(permit_id=entry_id)
        else:
            return None, f"动作「{action}」不属于放行回执可执行范围"
        # 遵循基类契约：失败时 entry 返回 None；完整判定建议直接调用对应方法。
        return (v if v.get("ok") else None), v.get("message", "")

    # ── 基础工具 ────────────────────────────────────────────────

    @property
    def _batches(self) -> list[dict[str, Any]]:
        return store.rows("release_batches")

    @property
    def _receipts(self) -> list[dict[str, Any]]:
        return store.rows("release_receipts")

    @property
    def _claims(self) -> list[dict[str, Any]]:
        return store.rows("release_claims")

    @property
    def _todos(self) -> list[dict[str, Any]]:
        return store.rows("delivery_todos")

    @property
    def _records(self) -> list[dict[str, Any]]:
        return store.rows("delivery_records")

    @staticmethod
    def _receiver_key(receiver: Any) -> str:
        return str(receiver or "").strip().casefold()

    def reset(self) -> dict[str, Any]:
        """清空放行域数据（主要给测试/演示复位用）。"""
        with _LOCK:
            for name in ("release_batches", "release_receipts", "release_claims",
                         "delivery_todos", "delivery_records"):
                store.rows(name).clear()
        return {"ok": True, "message": "放行域数据已复位"}

    def __getattr__(self, name: str):
        # 仅在正常属性查找失败时触发；把近义方法名分派到规范实现
        canonical = _fuzzy_method(name)
        if canonical is None:
            raise AttributeError(name)
        return getattr(self, canonical)

    # ── 参数归一化 ──────────────────────────────────────────────

    def _normalize_kwargs(self, args: tuple[Any, ...], kwargs: dict[str, Any],
                          positional: tuple[str, ...]) -> dict[str, Any]:
        """允许 svc 方法同时接受位置参数与中英文关键字。"""
        payload: dict[str, Any] = {}
        for key, value in kwargs.items():
            if value is not None:
                payload[key] = value
        for name, value in zip(positional, args):
            if value is not None:
                payload.setdefault(name, value)
        return payload

    @staticmethod
    def _id_of(value: Any) -> Any:
        """dict 行取 id，其余原样返回。"""
        if isinstance(value, dict):
            return value.get("id") or value.get("export_id") or value.get("batch_id")
        return value

    # ── 批次版本 ────────────────────────────────────────────────

    def _export_row(self, export_id: Any) -> dict[str, Any] | None:
        if export_id is None or export_id == "":
            return None
        if isinstance(export_id, dict):
            if export_id.get("id") is not None:
                found = store.find("export_dataset", int(export_id["id"]))
                if found:
                    return found
            export_id = export_id.get("id") or export_id.get("任务编号")
        try:
            eid = int(export_id)
        except (TypeError, ValueError):
            # 也允许按任务编号查
            rows = store.rows("export_dataset")
            for row in rows:
                if str(row.get("任务编号", "")) == str(export_id):
                    return row
            return None
        return store.find("export_dataset", eid)

    def create_batch(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """登记一个导出批次的初始版本 R1。重复以 batch_key/export_id 调用时返回既有当前版本。"""
        p = self._normalize_kwargs(args, kwargs, ("batch_no", "export_id", "receiver"))
        with _LOCK:
            return self._ensure_batch(p)

    def _ensure_batch(self, p: dict[str, Any]) -> dict[str, Any]:
        export_id = _first(p, _EXPORT_KEYS)
        batch_no = _first(p, _BATCH_KEYS)
        export = self._export_row(export_id) if export_id is not None else None
        if export is not None and batch_no is None:
            batch_no = export.get("任务编号") or f"EXPORT-{export.get('id')}"
        if batch_no is None:
            batch_no = f"BATCH-{_next_id(self._batches):04d}"
        batch_key = str(_first(p, ("batch_key", "key"), str(batch_no)))

        existing_current = next(
            (b for b in self._batches
             if b.get("batch_key") == batch_key and b.get("current")),
            None,
        )
        # 若指定了 export_id 但 key 不同，也按 export 找一次
        if existing_current is None and export is not None:
            existing_current = next(
                (b for b in self._batches
                 if b.get("export_id") == export.get("id") and b.get("current")),
                None,
            )
        if existing_current is not None:
            return existing_current

        row_count = p.get("row_count") or (export or {}).get("_exported_rows") or 0
        file_path = p.get("file_path") or (export or {}).get("文件路径") or ""
        digest = p.get("digest") or p.get("content_digest") or (export or {}).get("_digest") or ""
        row = {
            "id": _next_id(self._batches),
            "kind": "release_batch_version",
            "batch_key": batch_key,
            "batch_no": str(batch_no),
            "export_id": export.get("id") if export else (_safe_int(export_id)),
            "task_no": (export or {}).get("任务编号", str(batch_no)),
            "revision": 1,
            "revision_label": "R1",
            "version": "R1",
            "status": CURRENT,
            "status_zh": "当前版本",
            "current": True,
            "corrected_from_id": None,
            "correction_reason": "",
            "file_path": file_path,
            "digest": digest,
            "row_count": row_count,
            "snapshot": {
                "batch_no": str(batch_no),
                "task_no": (export or {}).get("任务编号", str(batch_no)),
                "file_path": file_path,
                "digest": digest,
                "row_count": row_count,
                "export_status": (export or {}).get("status", ""),
                "导出格式": (export or {}).get("导出格式", ""),
            },
            "created_by": p.get("owner") or p.get("operator") or p.get("负责人") or "交接负责人",
            "created_at": time.time(),
            "iso": _now_iso(),
        }
        self._batches.append(row)
        store.pipeline_trace("export_dataset", "release_gate",
                             row.get("export_id") or row["id"], row["id"],
                             "batch_created", {"batch_key": batch_key, "revision": "R1"})
        return row

    def list_batches(self, batch_key: str | None = None, **filters: Any) -> list[dict[str, Any]]:
        rows = list(self._batches)
        key = batch_key or filters.get("batch_no") or filters.get("batch")
        if key:
            rows = [b for b in rows if b.get("batch_key") == key or b.get("batch_no") == key]
        export_id = filters.get("export_id")
        if export_id is not None:
            rows = [b for b in rows if str(b.get("export_id")) == str(export_id)]
        return [self.serialize_batch(b) for b in rows]

    def get_batch(self, ref: Any = None, **kwargs: Any) -> dict[str, Any] | None:
        with _LOCK:
            return self._resolve_batch(ref, kwargs)

    def _resolve_batch(self, ref: Any = None, kwargs: dict[str, Any] | None = None) -> dict[str, Any] | None:
        kwargs = kwargs or {}
        if isinstance(ref, dict):
            if ref.get("batch_key") and ref.get("revision") is not None:
                found = next((b for b in self._batches
                              if b.get("batch_key") == ref["batch_key"]
                              and b.get("revision") == _revision_of(ref.get("revision"))), None)
                if found:
                    return found
            ref = ref.get("id") or ref.get("batch_id") or ref.get("batch_no")
        if ref is not None and not isinstance(ref, (int, float)):
            text = str(ref)
            found = next((b for b in self._batches if str(b.get("id")) == text), None)
            if found:
                return found
            found = next((b for b in self._batches if b.get("batch_key") == text and b.get("current")), None)
            if found:
                return found
            found = next((b for b in self._batches if b.get("batch_no") == text and b.get("current")), None)
            if found:
                return found
            rev = _revision_of(kwargs.get("revision") or kwargs.get("version"))
            if rev is not None:
                found = next((b for b in self._batches
                              if (b.get("batch_key") == text or b.get("batch_no") == text)
                              and b.get("revision") == rev), None)
                if found:
                    return found
        if isinstance(ref, (int, float)) or (isinstance(ref, str) and ref.isdigit()):
            found = store.find("release_batches", int(ref))
            if found:
                return found
        # 从 kwargs 解析
        bid = _first(kwargs, ("batch_id", "id"))
        if bid is not None:
            found = store.find("release_batches", _safe_int(bid) or -1)
            if found:
                return found
        export_id = _first(kwargs, _EXPORT_KEYS)
        bkey = _first(kwargs, _BATCH_KEYS)
        rev = _revision_of(_first(kwargs, _REVISION_KEYS))
        candidates = list(self._batches)
        if bkey is not None:
            candidates = [b for b in candidates if b.get("batch_key") == bkey or b.get("batch_no") == bkey]
        if export_id is not None:
            candidates = [b for b in candidates if str(b.get("export_id")) == str(export_id)]
        if rev is not None:
            specific = [b for b in candidates if b.get("revision") == rev]
            if specific:
                return specific[0]
        current = [b for b in candidates if b.get("current")]
        return current[0] if current else (candidates[-1] if candidates else None)

    def correct_batch(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """交接人更正批次：冻结旧版本，生成 revision+1 的新版本（R1->R2）。"""
        p = self._normalize_kwargs(args, kwargs, ("ref", "reason", "operator"))
        with _LOCK:
            idem = _first(p, _IDEM_KEYS)
            if idem:
                prior = next((b for b in self._batches if b.get("idempotency_key") == idem), None)
                if prior is not None:
                    return {"ok": True, "allowed": True, "idempotent_replay": True,
                            "message": f"重复的更正请求，返回既有版本 {prior['revision_label']}",
                            "batch": self.serialize_batch(prior)}
            ref = p.pop("ref", None)
            explicit = _first(p, _BATCH_KEYS + _EXPORT_KEYS + ("batch_id", "id"))
            if ref is not None and explicit is None:
                # 位置首参可能是：批次版本 id、batch_key/batch_no、或导出任务 id
                if isinstance(ref, dict):
                    if ref.get("batch_key"):
                        p["batch_key"] = ref["batch_key"]
                    ref = ref.get("id") or ref.get("export_id")
                text = str(ref)
                by_key = next((b for b in self._batches
                               if b.get("batch_key") == text or b.get("batch_no") == text), None)
                if by_key is not None:
                    p["batch_key"] = by_key["batch_key"]
                elif isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
                    num = int(ref)
                    if store.find("release_batches", num) is not None:
                        p["batch_id"] = num
                    elif self._export_row(num) is not None:
                        p["export_id"] = num
                    else:
                        p["batch_id"] = num
                else:
                    p["batch_key"] = text
            old = self._resolve_batch(None, p)
            if old is None:
                # 允许直接对一个尚不存在批次的导出任务做更正 -> 先建 R1 再更正为 R2
                if _first(p, _EXPORT_KEYS + _BATCH_KEYS) is not None:
                    old = self._ensure_batch(p)
                else:
                    return {"ok": False, "allowed": False,
                            "message": "找不到需要更正的导出批次", "code": "BATCH_NOT_FOUND"}
            new_rev = int(old.get("revision", 1)) + 1
            # 幂等：如果该父版本已经更正过，直接返回已生成的新版本（重复提交更正）
            existing_child = next((b for b in self._batches
                                   if b.get("corrected_from_id") == old.get("id")), None)
            if existing_child is not None and not p.get("force"):
                return {"ok": True, "allowed": True, "idempotent_replay": True,
                        "message": f"批次已更正为 {existing_child['revision_label']}，返回既有新版本",
                        "batch": self.serialize_batch(existing_child)}
            old["current"] = False
            old["status"] = CORRECTED
            old["status_zh"] = "已更正"
            file_path = p.get("file_path", old.get("file_path"))
            digest = p.get("digest") or p.get("content_digest") or (
                f"sha256:{uuid.uuid4().hex[:16]}" if p.get("change_content", True) else old.get("digest"))
            new = {
                "id": _next_id(self._batches),
                "kind": "release_batch_version",
                "idempotency_key": idem,
                "batch_key": old["batch_key"],
                "batch_no": old["batch_no"],
                "export_id": old.get("export_id"),
                "task_no": old.get("task_no"),
                "revision": new_rev,
                "revision_label": f"R{new_rev}",
                "version": f"R{new_rev}",
                "status": CURRENT,
                "status_zh": "当前版本",
                "current": True,
                "corrected_from_id": old["id"],
                "correction_reason": p.get("reason") or p.get("更正原因") or "交接人更正批次",
                "file_path": file_path,
                "digest": digest,
                "row_count": p.get("row_count", old.get("row_count")),
                "snapshot": {
                    "batch_no": old["batch_no"],
                    "task_no": old.get("task_no"),
                    "file_path": file_path,
                    "digest": digest,
                    "row_count": p.get("row_count", old.get("row_count")),
                    "correction_reason": p.get("reason") or "交接人更正批次",
                    "corrected_from": old["revision_label"],
                },
                "created_by": p.get("operator") or p.get("owner") or p.get("交接人") or "交接负责人",
                "created_at": time.time(),
                "iso": _now_iso(),
            }
            self._batches.append(new)
            # 同批次旧待办打上版本漂移标记（不改事实、不删数据）
            for todo in self._todos:
                if todo.get("batch_key") == old["batch_key"] and todo.get("revision") == old["revision"]:
                    todo["version_drift"] = True
                    todo["current_revision_label"] = new["revision_label"]
            store.pipeline_trace("export_dataset", "release_gate",
                                 old.get("export_id") or old["id"], new["id"],
                                 "batch_corrected",
                                 {"batch_key": old["batch_key"],
                                  "from": old["revision_label"], "to": new["revision_label"]})
            self._mirror_to_export(batch=new)
            return {"ok": True, "allowed": True,
                    "message": f"批次已由 {old['revision_label']} 更正为 {new['revision_label']}，旧回执保留可查证",
                    "old_batch": self.serialize_batch(old),
                    "batch": self.serialize_batch(new)}

    def serialize_batch(self, b: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in b.items()}

    # ── 放行回执（发起放行） ────────────────────────────────────

    def initiate_release(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """交接负责人选择确定的批次版本与接收方发起放行，生成可供核销的回执。"""
        p = self._normalize_kwargs(
            args, kwargs, ("export_id", "receiver", "batch_no", "owner"))
        with _LOCK:
            receiver = _first(p, _RECEIVER_KEYS)
            if not receiver or not str(receiver).strip():
                return {"ok": False, "allowed": False, "can_send": False,
                        "needs_reconfirm": True, "code": "RECEIVER_REQUIRED",
                        "message": "发起放行必须指定明确的接收方，放行范围要指向确定的批次与接收方"}
            # 位置首参约定为 export_id；统一交给 kwargs 解析，避免与 release_batches 数字 id 混淆
            if "export_id" in p:
                p["export_id"] = self._id_of(p["export_id"])
            if p.get("batch") is not None:
                p.setdefault("batch_id", self._id_of(p["batch"])) if isinstance(p["batch"], dict) else None
            batch = self._resolve_batch(None, p)
            if batch is None:
                if _first(p, _BATCH_KEYS + _EXPORT_KEYS) is None:
                    return {"ok": False, "allowed": False, "can_send": False,
                            "needs_reconfirm": True, "code": "BATCH_REQUIRED",
                            "message": "发起放行必须选择确定的导出批次"}
                batch = self._ensure_batch(p)
            receiver = str(receiver).strip()
            rkey = self._receiver_key(receiver)
            # 幂等：同一 (批次版本, 接收方) 已有未驳回回执时直接返回，避免重复放行产生多个身份
            existing = next((r for r in self._receipts
                             if r.get("batch_key") == batch["batch_key"]
                             and r.get("revision") == batch["revision"]
                             and r.get("receiver_key") == rkey
                             and r.get("status") != STATUS_REJECTED), None)
            if existing is not None and not p.get("force_new"):
                verdict = self._verdict(existing, code="RELEASE_EXISTS",
                                        message="该批次版本对该接收方已有放行回执，返回原许可身份")
                verdict.update({"ok": True, "idempotent_replay": True,
                                "receipt": verdict["permit"]})
                return verdict
            seq = _next_id(self._receipts)
            permit_id = f"HX-{seq:06d}"
            receipt = {
                "id": seq,
                "kind": "release_receipt",
                "permit_id": permit_id,
                "receipt_no": permit_id,
                "token": uuid.uuid4().hex,
                "batch_id": batch["id"],
                "batch_key": batch["batch_key"],
                "batch_no": batch["batch_no"],
                "export_id": batch.get("export_id"),
                "task_no": batch.get("task_no"),
                "revision": batch["revision"],
                "revision_label": batch["revision_label"],
                "version": batch["revision_label"],
                "receiver": receiver,
                "receiver_key": rkey,
                "scope": f"{batch['batch_key']}@{batch['revision_label']} -> {receiver}",
                "status": STATUS_RELEASED,
                "issued_by": p.get("owner") or p.get("operator") or p.get("负责人") or "交接负责人",
                "owner": p.get("owner") or "交接负责人",
                "created_at": time.time(),
                "iso": _now_iso(),
                "claim_ids": [],
                "approval": None,
                "redeemed_at": None,
                "delivery_id": None,
                "frozen_batch": dict(batch.get("snapshot") or {}),
            }
            self._receipts.append(receipt)
            todo = self._ensure_todo_for_receipt(receipt, p)
            store.pipeline_trace("export_dataset", "delivery_pipe",
                                 batch.get("export_id") or batch["id"], todo["id"],
                                 "release_issued",
                                 {"permit_id": permit_id, "scope": receipt["scope"]})
            self._mirror_to_export(batch=batch, receipt=receipt)
            verdict = self._verdict(receipt, code="RELEASED",
                                    message=f"已发起放行：{receipt['scope']}，回执 {permit_id} 可在投递待发送行核销")
            verdict.update({"ok": True, "receipt": verdict["permit"], "todo": todo})
            return verdict

    # 语义别名
    def release_batch(self, *a: Any, **k: Any) -> dict[str, Any]:
        return self.initiate_release(*a, **k)

    create_release = release_batch
    issue_receipt = release_batch
    create_receipt = release_batch
    request_release = release_batch
    发起放行 = initiate_release

    def _mirror_to_export(self, batch: dict[str, Any] | None = None,
                          receipt: dict[str, Any] | None = None) -> None:
        """把最新许可身份摘要回写到 export_dataset 行，方便从导出行直接读到回执标记。"""
        try:
            export_id = (receipt or {}).get("export_id") or (batch or {}).get("export_id")
            exp = store.find("export_dataset", int(export_id)) if export_id not in (None, "") else None
            if exp is None:
                task_no = (batch or {}).get("task_no") or (receipt or {}).get("task_no")
                if task_no:
                    exp = next((r for r in store.rows("export_dataset")
                                if r.get("任务编号") == task_no), None)
            if exp is None:
                return
            target_batch = batch
            if target_batch is None and receipt is not None:
                target_batch = self._current_batch(receipt)
            if target_batch is not None:
                exp["_release_batch_key"] = target_batch.get("batch_key")
                exp["_current_revision"] = target_batch.get("revision")
                exp["_current_version"] = target_batch.get("revision_label")
                exp["可发送版本"] = target_batch.get("revision_label")
            if receipt is not None:
                exp["_latest_permit_id"] = receipt.get("permit_id")
                exp["放行回执"] = receipt.get("permit_id")
                exp["_release_status"] = receipt.get("status")
            ids = [r.get("permit_id") for r in self._receipts if r.get("export_id") == exp.get("id")]
            if ids:
                exp["_release_permit_ids"] = ids
        except (TypeError, ValueError):
            return

    def list_receipts(self, **filters: Any) -> list[dict[str, Any]]:
        with _LOCK:
            rows = list(self._receipts)
            receiver = _first(filters, _RECEIVER_KEYS)
            if receiver:
                key = self._receiver_key(receiver)
                rows = [r for r in rows if r.get("receiver_key") == key]
            bkey = _first(filters, _BATCH_KEYS)
            if bkey:
                rows = [r for r in rows if r.get("batch_key") == bkey or r.get("batch_no") == bkey]
            status = filters.get("status")
            if status:
                rows = [r for r in rows if r.get("status") == status]
            return [self._verdict(r)["permit"] for r in rows]

    def get_receipt(self, ref: Any = None, **kwargs: Any) -> dict[str, Any] | None:
        with _LOCK:
            receipt = self._resolve_receipt(ref, kwargs)
            return self._verdict(receipt)["permit"] if receipt else None

    def _resolve_receipt(self, ref: Any = None, kwargs: dict[str, Any] | None = None) -> dict[str, Any] | None:
        kwargs = kwargs or {}
        if isinstance(ref, dict):
            kwargs = {**ref, **kwargs}
            ref = ref.get("permit_id") or ref.get("receipt_no") or ref.get("id")
        permit = _first(kwargs, _PERMIT_KEYS)
        if isinstance(permit, dict):
            permit = permit.get("permit_id") or permit.get("receipt_no") or permit.get("id")
        if permit is not None:
            text = str(permit)
            found = next((r for r in self._receipts
                          if r.get("permit_id") == text or r.get("receipt_no") == text
                          or r.get("token") == text or str(r.get("id")) == text), None)
            if found:
                return found
        if ref is not None:
            text = str(ref)
            found = next((r for r in self._receipts
                          if r.get("permit_id") == text or r.get("receipt_no") == text
                          or r.get("token") == text or str(r.get("id")) == text), None)
            if found:
                return found
        # 按作用域找：batch + receiver (+ revision)
        b = self._resolve_batch(_first(kwargs, _BATCH_KEYS + _EXPORT_KEYS), kwargs)
        receiver = _first(kwargs, _RECEIVER_KEYS)
        if b is not None and receiver:
            rev = _revision_of(_first(kwargs, _REVISION_KEYS)) or b["revision"]
            rkey = self._receiver_key(receiver)
            return next((r for r in self._receipts
                         if r.get("batch_key") == b["batch_key"]
                         and r.get("revision") == rev
                         and r.get("receiver_key") == rkey
                         and r.get("status") != STATUS_REJECTED), None)
        if b is not None:
            rev = _revision_of(_first(kwargs, _REVISION_KEYS))
            candidates = [r for r in self._receipts if r.get("batch_key") == b["batch_key"]]
            if rev is not None:
                candidates = [r for r in candidates if r.get("revision") == rev]
            return candidates[-1] if candidates else None
        return None

    # ── 效力判定 / 许可身份 ─────────────────────────────────────

    def _current_batch(self, receipt: dict[str, Any]) -> dict[str, Any] | None:
        return next((b for b in self._batches
                     if b.get("batch_key") == receipt.get("batch_key") and b.get("current")), None)

    def _effective(self, receipt: dict[str, Any]) -> tuple[bool, str, dict[str, Any] | None]:
        """返回收据当前是否可核销，以及原因码与当前版本。"""
        current = self._current_batch(receipt)
        if receipt.get("status") == STATUS_REJECTED:
            return False, "RECEIPT_REJECTED", current
        if current is not None and int(receipt.get("revision", 0)) != int(current["revision"]):
            return False, "VERSION_SUPERSEDED", current
        if receipt.get("status") == STATUS_REDEEMED:
            return True, "ALREADY_REDEEMED", current  # 已核销本身是有效终态，允许幂等查询/重放
        return True, "EFFECTIVE", current

    def serialize_permit(self, receipt: dict[str, Any]) -> dict[str, Any]:
        effective, code, current = self._effective(receipt)
        already = receipt.get("status") == STATUS_REDEEMED
        needs_reconfirm = not effective and code == "VERSION_SUPERSEDED"
        redeemable = effective and not already and receipt.get("status") in (STATUS_RELEASED, STATUS_CLAIMED)
        can_send = effective  # 已核销视为已获得发送许可（可幂等取回投递身份）
        return {
            "id": receipt["id"],
            "permit_id": receipt["permit_id"],
            "receipt_no": receipt["receipt_no"],
            "token": receipt.get("token"),
            "scope": receipt.get("scope"),
            "batch_id": receipt.get("batch_id"),
            "batch_key": receipt.get("batch_key"),
            "batch_no": receipt.get("batch_no"),
            "export_id": receipt.get("export_id"),
            "task_no": receipt.get("task_no"),
            "revision": receipt.get("revision"),
            "revision_label": receipt.get("revision_label"),
            "version": receipt.get("revision_label"),
            "receiver": receipt.get("receiver"),
            "status": receipt.get("status"),
            "status_zh": receipt.get("status"),
            "effective": effective,
            "valid": effective,
            "is_effective": effective,
            "redeemable": redeemable,
            "can_redeem": redeemable,
            "can_send": can_send,
            "sendable": can_send,
            "needs_reconfirm": needs_reconfirm,
            "needs_review": needs_reconfirm,
            "needs_owner_confirmation": needs_reconfirm or code in ("RECEIPT_NOT_FOUND", "RELEASE_REQUIRED"),
            "reason_code": code,
            "validity_code": code,
            "current_revision": (current or {}).get("revision"),
            "current_revision_label": (current or {}).get("revision_label"),
            "current_version": (current or {}).get("revision_label"),
            "current_batch_id": (current or {}).get("id"),
            "current_permit_id": self._current_permit_id(receipt, current),
            "verifiable": True,  # 旧回执永远可原样查证
            "frozen_batch": receipt.get("frozen_batch", {}),
            "issued_by": receipt.get("issued_by"),
            "iso": receipt.get("iso"),
            "claim_ids": list(receipt.get("claim_ids", [])),
            "approval": receipt.get("approval"),
            "redeemed_at": receipt.get("redeemed_at"),
            "delivery_id": receipt.get("delivery_id"),
        }

    def _current_permit_id(self, receipt: dict[str, Any], current: dict[str, Any] | None) -> str | None:
        if current is None:
            return None
        rkey = receipt.get("receiver_key")
        cur = next((r for r in self._receipts
                    if r.get("batch_key") == current.get("batch_key")
                    and r.get("revision") == current.get("revision")
                    and r.get("receiver_key") == rkey
                    and r.get("status") != STATUS_REJECTED), None)
        return cur.get("permit_id") if cur else None

    def _verdict(self, receipt: dict[str, Any] | None, *, code: str | None = None,
                 message: str = "", allowed: bool | None = None,
                 extra: dict[str, Any] | None = None) -> dict[str, Any]:
        permit = self.serialize_permit(receipt) if receipt else None
        if permit is not None:
            is_allowed = permit["can_send"] if allowed is None else allowed
            final_code = code or permit["reason_code"]
            needs = permit["needs_reconfirm"]
            current_permit = None
            if permit.get("current_permit_id"):
                cur_rcpt = next((r for r in self._receipts
                                 if r.get("permit_id") == permit["current_permit_id"]), None)
                current_permit = self.serialize_permit(cur_rcpt) if cur_rcpt else None
        else:
            is_allowed = False if allowed is None else allowed
            final_code = code or "RELEASE_REQUIRED"
            needs = True
            current_permit = None
        identity = None
        if permit is not None:
            identity = {
                "permit_id": permit["permit_id"],
                "receipt_no": permit["receipt_no"],
                "scope": permit["scope"],
                "batch_key": permit["batch_key"],
                "revision": permit["revision"],
                "revision_label": permit["revision_label"],
                "receiver": permit["receiver"],
                "status": permit["status"],
                "effective": permit["effective"],
                "can_send": permit["can_send"],
                "needs_reconfirm": permit["needs_reconfirm"],
            }
        verdict = {
            "ok": True,
            "allowed": is_allowed,
            "can_send": is_allowed,
            "sendable": is_allowed,
            "needs_reconfirm": needs,
            "needs_owner_confirmation": needs or receipt is None,
            "code": final_code,
            "reason_code": final_code,
            "message": message or self._default_message(final_code, permit),
            "permit": permit,
            "receipt": permit,
            "identity": identity,
            "permit_id": (permit or {}).get("permit_id"),
            "receipt_no": (permit or {}).get("receipt_no"),
            "scope": (permit or {}).get("scope"),
            "batch_key": (permit or {}).get("batch_key"),
            "batch_no": (permit or {}).get("batch_no"),
            "revision": (permit or {}).get("revision"),
            "revision_label": (permit or {}).get("revision_label"),
            "version": (permit or {}).get("revision_label"),
            "receiver": (permit or {}).get("receiver"),
            "receipt_status": (permit or {}).get("status"),
            "submitted_permit_id": (permit or {}).get("permit_id"),
            "current_permit": current_permit,
        }
        if extra:
            verdict.update(extra)
        return verdict

    @staticmethod
    def _default_message(code: str, permit: dict[str, Any] | None) -> str:
        if code == "VERSION_SUPERSEDED" and permit:
            return (f"回执 {permit['permit_id']} 指向 {permit['revision_label']}，"
                    f"批次已更正为 {permit.get('current_revision_label')}；"
                    f"旧签收/回执仍可原样查证，但该版本不能启动投递，"
                    f"需要负责人对 {permit.get('current_revision_label')} 重新复核放行")
        if code in ("RELEASE_REQUIRED", "RECEIPT_NOT_FOUND"):
            return "缺少指向当前确定批次与接收方的有效放行回执，下载预检或导出完成不能代替放行；请交接负责人发起放行"
        if code == "RECEIVER_MISMATCH":
            return "接收方与放行回执不一致，新接收不能借用旧批次或许可证，请负责人向该接收方重新放行"
        if code == "ALREADY_REDEEMED":
            return "该回执已核销，重复提交按原结果返回，不会重复发送"
        if code == "RECEIPT_REJECTED":
            return "该放行申请已被签批驳回，请重新提请负责人确认"
        return "放行许可状态正常"

    def evaluate(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """只判定不落库：返回某批次/接收方/许可身份当前能否发送。"""
        p = self._normalize_kwargs(args, kwargs, ("permit_id", "receiver", "batch_no", "revision"))
        with _LOCK:
            receipt = self._resolve_receipt(p.get("ref"), p)
            if receipt is None:
                denied = self._verdict(None, code="RELEASE_REQUIRED", allowed=False,
                                       message="找不到匹配当前批次与接收方的放行回执")
                return denied
            receiver = _first(p, _RECEIVER_KEYS)
            if receiver and self._receiver_key(receiver) != receipt.get("receiver_key"):
                return self._receiver_mismatch(receipt, receiver)
            return self._verdict(receipt)

    can_deliver = evaluate
    check_permit = evaluate
    verify = evaluate

    def _receiver_mismatch(self, receipt: dict[str, Any], receiver: Any) -> dict[str, Any]:
        v = self._verdict(receipt, code="RECEIVER_MISMATCH", allowed=False)
        v["ok"] = False
        v["message"] = f"提交接收方「{receiver}」与回执接收方「{receipt.get('receiver')}」不一致；新接收不能借用旧批次的许可"
        v["needs_reconfirm"] = True
        v["needs_owner_confirmation"] = True
        return v

    # ── 领取 / 签收（幂等） ─────────────────────────────────────

    def claim_receipt(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """接收方领取/签收。同一 (回执, 接收方) 或同一幂等键的重复请求返回同一领取身份。"""
        p = self._normalize_kwargs(args, kwargs, ("permit_id", "receiver", "claimant"))
        with _LOCK:
            idem = _first(p, _IDEM_KEYS)
            if idem:
                prior = next((c for c in self._claims if c.get("idempotency_key") == idem), None)
                if prior is not None:
                    return self._claim_replay(prior, "重复的领取请求（相同幂等键），返回原领取身份")
            receipt = self._resolve_receipt(p.get("ref"), p)
            if receipt is None:
                denied = self._verdict(None, code="RECEIPT_NOT_FOUND", allowed=False,
                                       message="领取失败：找不到对应的放行回执/许可身份，请负责人先发起放行")
                denied["ok"] = False
                return denied
            receiver = _first(p, _RECEIVER_KEYS) or receipt.get("receiver")
            if self._receiver_key(receiver) != receipt.get("receiver_key"):
                return self._receiver_mismatch(receipt, receiver)
            # 自然幂等：同回执同接收方已有领取记录 -> 返回原记录（重复提交原领取请求）
            prior = next((c for c in self._claims
                          if c.get("receipt_id") == receipt["id"]
                          and c.get("receiver_key") == receipt.get("receiver_key")), None)
            if prior is not None:
                if idem and not prior.get("idempotency_key"):
                    prior["idempotency_key"] = idem
                return self._claim_replay(prior, "该接收方已领取/签收过此回执，重复提交返回原领取身份")
            claim = {
                "id": _next_id(self._claims),
                "claim_no": f"QS-{_next_id(self._claims):06d}",
                "idempotency_key": idem,
                "receipt_id": receipt["id"],
                "permit_id": receipt["permit_id"],
                "batch_key": receipt["batch_key"],
                "batch_no": receipt["batch_no"],
                "revision": receipt["revision"],
                "revision_label": receipt["revision_label"],
                "receiver": receipt["receiver"],
                "receiver_key": receipt["receiver_key"],
                "claimant": p.get("claimant") or p.get("operator") or receiver,
                "result": p.get("result") or "签收",
                "comment": p.get("comment") or p.get("意见") or "",
                "created_at": time.time(),
                "iso": _now_iso(),
            }
            self._claims.append(claim)
            receipt["claim_ids"].append(claim["id"])
            if receipt["status"] == STATUS_RELEASED:
                receipt["status"] = STATUS_CLAIMED
            self._writeback_todos(receipt, claim=claim)
            store.pipeline_trace("delivery_pipe", "release_gate", claim["id"], receipt["id"],
                                 "receipt_claimed", {"permit_id": receipt["permit_id"]})
            verdict = self._verdict(receipt)
            base_message = f"接收方「{receipt['receiver']}」已领取 {receipt['revision_label']} 回执 {receipt['permit_id']}"
            if not verdict.get("can_send"):
                base_message += "。" + verdict.get("message", "")
            verdict.update({
                "ok": True,
                "message": base_message,
                "claim": claim,
                "claim_id": claim["id"],
                "claim_no": claim["claim_no"],
            })
            return verdict

    claim = claim_receipt
    sign_for = claim_receipt
    sign = claim_receipt
    receive = claim_receipt
    领取 = claim_receipt
    签收 = claim_receipt

    def _claim_replay(self, claim: dict[str, Any], message: str) -> dict[str, Any]:
        receipt = next((r for r in self._receipts if r.get("id") == claim.get("receipt_id")), None)
        verdict = self._verdict(receipt) if receipt else self._verdict(None)
        full_message = message
        if receipt is not None and not verdict.get("can_send"):
            full_message += "。" + verdict.get("message", "")
        verdict.update({
            "ok": True,
            "idempotent_replay": True,
            "duplicate": True,
            "replayed": True,
            "message": full_message,
            "claim": claim,
            "claim_id": claim["id"],
            "claim_no": claim["claim_no"],
        })
        return verdict

    def list_claims(self, **filters: Any) -> list[dict[str, Any]]:
        with _LOCK:
            rows = list(self._claims)
            permit = _first(filters, _PERMIT_KEYS)
            if permit:
                rows = [c for c in rows if c.get("permit_id") == str(permit)
                        or str(c.get("receipt_id")) == str(permit)]
            bkey = _first(filters, _BATCH_KEYS)
            if bkey:
                rows = [c for c in rows if c.get("batch_key") == bkey or c.get("batch_no") == bkey]
            return [dict(c) for c in rows]

    # ── 签批（结果回写投递待办） ────────────────────────────────

    def approve_receipt(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """签批通过/驳回；结果回写到关联的投递待发送行，并返回版本是否已漂移。"""
        p = self._normalize_kwargs(args, kwargs, ("permit_id", "decision", "approver", "comment"))
        with _LOCK:
            idem = _first(p, _IDEM_KEYS)
            if idem:
                prior = next((a for a in self._claims if a.get("idempotency_key") == idem
                              and a.get("kind") == "approval"), None)
                if prior is not None:
                    receipt = self._resolve_receipt(prior.get("permit_id"), {})
                    verdict = self._verdict(receipt) if receipt else self._verdict(None)
                    verdict.update({"ok": True, "idempotent_replay": True, "approval": prior,
                                    "message": "重复的签批请求，返回原签批结果"})
                    return verdict
            receipt = self._resolve_receipt(p.get("ref"), p)
            if receipt is None:
                denied = self._verdict(None, code="RECEIPT_NOT_FOUND", allowed=False,
                                       message="签批失败：找不到对应放行回执")
                denied["ok"] = False
                return denied
            decision_raw = str(_first(p, ("decision", "result", "action", "签批结果", "决定"), "通过"))
            approved = decision_raw in ("通过", "同意", "批准", "approved", "approve", "yes", "true", "1")
            approval = {
                "kind": "approval",
                "id": _next_id(self._claims),
                "idempotency_key": idem,
                "receipt_id": receipt["id"],
                "permit_id": receipt["permit_id"],
                "decision": "同意" if approved else "驳回",
                "approved": approved,
                "approver": p.get("approver") or p.get("operator") or p.get("签批人") or "交接负责人",
                "comment": p.get("comment") or p.get("意见") or "",
                "batch_key": receipt["batch_key"],
                "revision": receipt["revision"],
                "revision_label": receipt["revision_label"],
                "receiver": receipt["receiver"],
                "created_at": time.time(),
                "iso": _now_iso(),
            }
            self._claims.append(approval)
            receipt["approval"] = approval
            if not approved:
                receipt["status"] = STATUS_REJECTED
            self._writeback_todos(receipt, approval=approval)
            verdict = self._verdict(receipt)
            drifted = verdict.get("code") == "VERSION_SUPERSEDED"
            verdict.update({
                "ok": True,
                "approval": approval,
                "version_changed": drifted,
                "review_required_version": verdict.get("current_revision_label") if drifted else None,
                "message": (f"签批已{approval['decision']}并回写投递待办。"
                            + (f"注意：批次已变更，当前需复核版本为 {verdict.get('current_revision_label')}；"
                               f"原 {receipt['revision_label']} 回执仍可打开查证。"
                               if drifted else "放行版本与当前批次一致，可在投递待发送行核销。")),
            })
            return verdict

    approve = approve_receipt
    sign_off = approve_receipt
    签批 = approve_receipt
    签批通过 = approve_receipt

    def reject_receipt(self, *a: Any, **k: Any) -> dict[str, Any]:
        k["decision"] = "驳回"
        return self.approve_receipt(*a, **k)

    # ── 投递待办 ────────────────────────────────────────────────

    def _ensure_todo_for_receipt(self, receipt: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
        pipe_id = p.get("pipe_id") or p.get("delivery_pipe_id")
        todo = next((t for t in self._todos if t.get("permit_id") == receipt["permit_id"]), None)
        if todo is None:
            todo = {
                "id": _next_id(self._todos),
                "kind": "delivery_todo",
                "pipe_id": _safe_int(pipe_id),
                "pipe_name": p.get("pipe_name") or "",
                "title": f"{receipt['batch_no']} {receipt['revision_label']} -> {receipt['receiver']}",
                "batch_key": receipt["batch_key"],
                "batch_no": receipt["batch_no"],
                "export_id": receipt.get("export_id"),
                "revision": receipt["revision"],
                "revision_label": receipt["revision_label"],
                "receiver": receipt["receiver"],
                "receiver_key": receipt["receiver_key"],
                "permit_id": receipt["permit_id"],
                "status": TODO_PENDING,
                "approval_status": (receipt.get("approval") or {}).get("decision", "待签批"),
                "签批结果": (receipt.get("approval") or {}).get("decision", "待签批"),
                "claim_status": "待领取",
                "version_drift": False,
                "delivery_id": None,
                "created_at": time.time(),
                "iso": _now_iso(),
            }
            self._todos.append(todo)
        return todo

    def create_todo(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """登记一条投递待发送行（可先不绑回执；核销/启动时再校验许可）。"""
        p = self._normalize_kwargs(args, kwargs, ("pipe_id", "batch_no", "receiver", "permit_id"))
        with _LOCK:
            receiver = _first(p, _RECEIVER_KEYS)
            b = self._resolve_batch(_first(p, _BATCH_KEYS + _EXPORT_KEYS), p)
            receipt = self._resolve_receipt(_first(p, _PERMIT_KEYS), p)
            if receipt is not None:
                todo = self._ensure_todo_for_receipt(receipt, p)
                return {"ok": True, "todo": self.serialize_todo(todo),
                        "message": "待发送行已关联放行回执"}
            if not receiver:
                return {"ok": False, "message": "待发送行至少需要接收方或有效回执", "code": "RECEIVER_REQUIRED"}
            seq = _next_id(self._todos)
            todo = {
                "id": seq,
                "kind": "delivery_todo",
                "pipe_id": _safe_int(_first(p, ("pipe_id", "delivery_pipe_id", "entry_id"))),
                "pipe_name": p.get("pipe_name") or "",
                "title": p.get("title") or f"{_first(p, _BATCH_KEYS, (b or {}).get('batch_no', '未指定批次'))} -> {receiver}",
                "batch_key": (b or {}).get("batch_key") or str(_first(p, _BATCH_KEYS, "")),
                "batch_no": (b or {}).get("batch_no") or str(_first(p, _BATCH_KEYS, "")),
                "export_id": (b or {}).get("export_id") or _safe_int(_first(p, _EXPORT_KEYS)),
                "revision": (b or {}).get("revision"),
                "revision_label": (b or {}).get("revision_label"),
                "receiver": str(receiver),
                "receiver_key": self._receiver_key(receiver),
                "permit_id": None,
                "status": TODO_PENDING,
                "approval_status": "待签批",
                "签批结果": "待签批",
                "claim_status": "待领取",
                "version_drift": False,
                "delivery_id": None,
                "created_at": time.time(),
                "iso": _now_iso(),
            }
            self._todos.append(todo)
            return {"ok": True, "todo": self.serialize_todo(todo), "message": "已登记投递待发送行"}

    def list_todos(self, **filters: Any) -> list[dict[str, Any]]:
        with _LOCK:
            rows = list(self._todos)
            pipe_id = filters.get("pipe_id") or filters.get("entry_id")
            if pipe_id is not None and str(pipe_id) != "":
                rows = [t for t in rows if str(t.get("pipe_id")) == str(pipe_id)]
            status = filters.get("status")
            if status:
                rows = [t for t in rows if t.get("status") == status]
            receiver = _first(filters, _RECEIVER_KEYS)
            if receiver:
                key = self._receiver_key(receiver)
                rows = [t for t in rows if t.get("receiver_key") == key]
            return [self.serialize_todo(t) for t in rows]

    def get_todo(self, todo_id: int) -> dict[str, Any] | None:
        with _LOCK:
            todo = store.find("delivery_todos", int(todo_id))
            return self.serialize_todo(todo) if todo else None

    def serialize_todo(self, todo: dict[str, Any]) -> dict[str, Any]:
        data = dict(todo)
        receipt = next((r for r in self._receipts if r.get("permit_id") == todo.get("permit_id")), None)
        if receipt is None and todo.get("batch_key") and todo.get("receiver_key"):
            # 尝试按当前作用域找当前有效回执，用于「标出实际可发送版本」
            current = next((b for b in self._batches
                            if b.get("batch_key") == todo["batch_key"] and b.get("current")), None)
            if current is not None:
                receipt = next((r for r in self._receipts
                                if r.get("batch_key") == current["batch_key"]
                                and r.get("revision") == current["revision"]
                                and r.get("receiver_key") == todo["receiver_key"]
                                and r.get("status") != STATUS_REJECTED), None)
        verdict = self._verdict(receipt) if receipt else self._verdict(None)
        data["verdict"] = {k: verdict[k] for k in
                           ("allowed", "can_send", "sendable", "needs_reconfirm", "code", "message",
                            "permit_id", "current_permit")}
        data["can_send"] = verdict["can_send"]
        data["sendable"] = verdict["sendable"]
        data["needs_reconfirm"] = verdict["needs_reconfirm"]
        data["marker"] = (f"回执 {receipt['permit_id']} · {receipt['revision_label']} · 实际可发送"
                          if receipt and verdict["can_send"] and verdict["code"] != "ALREADY_REDEEMED"
                          else f"回执 {receipt['permit_id']} · {receipt['revision_label']} · 实际可发送（已核销/已启动投递，许可身份可重放）"
                          if receipt and verdict["code"] == "ALREADY_REDEEMED"
                          else "缺少当前版本的有效放行回执 · 实际不可发送/需负责人确认")
        if receipt:
            data["receipt"] = verdict["permit"]
        return data

    def _writeback_todos(self, receipt: dict[str, Any], *, claim: dict[str, Any] | None = None,
                         approval: dict[str, Any] | None = None) -> None:
        for todo in self._todos:
            if todo.get("permit_id") == receipt["permit_id"]:
                if claim is not None:
                    todo["claim_status"] = "已领取/已签收"
                if approval is not None:
                    todo["approval_status"] = approval["decision"]
                    todo["签批结果"] = approval["decision"]
                    todo["approver"] = approval.get("approver")
                    todo["approval_comment"] = approval.get("comment")
                    todo["approval_iso"] = approval.get("iso")

    # ── 核销 + 启动投递（CAS，只允许当前有效放行） ───────────────

    def redeem_receipt(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """投递服务核销回执。判定与落库在同一把锁内完成。"""
        p = self._normalize_kwargs(args, kwargs, ("permit_id", "receiver", "operator", "todo_id"))
        with _LOCK:
            idem = _first(p, _IDEM_KEYS)
            if idem:
                prior = next((d for d in self._records if d.get("idempotency_key") == idem), None)
                if prior is not None:
                    return self._delivery_replay(prior, "重复的核销/投递请求（相同幂等键），返回原投递身份")
            todo = None
            todo_id = p.get("todo_id")
            if todo_id is not None:
                todo = store.find("delivery_todos", _safe_int(todo_id) or -1)
            receipt = self._resolve_receipt(p.get("ref"), p)
            if receipt is None and todo is not None and todo.get("permit_id"):
                receipt = self._resolve_receipt(todo["permit_id"], {})
            if receipt is None:
                # 允许用 批次+版本+接收方 隐式定位当前放行
                receipt = self._resolve_receipt(None, p)
            if receipt is None:
                verdict = self._verdict(None, code="RELEASE_REQUIRED", allowed=False)
                verdict.update({"ok": False,
                                "message": "核销失败：没有指向确定批次与接收方的有效放行回执；"
                                           "下载预检或内部生成完成不能代替放行，请负责人发起放行"})
                return verdict
            receiver = _first(p, _RECEIVER_KEYS)
            if receiver and self._receiver_key(receiver) != receipt.get("receiver_key"):
                return self._receiver_mismatch(receipt, receiver)
            if todo is None:
                todo = next((t for t in self._todos if t.get("permit_id") == receipt["permit_id"]), None)
            effective, code, current = self._effective(receipt)
            if code == "ALREADY_REDEEMED":
                record = next((d for d in self._records
                               if d.get("permit_id") == receipt["permit_id"]), None)
                verdict = self._verdict(receipt)
                verdict.update({"ok": True, "idempotent_replay": True, "already_redeemed": True,
                                "message": "该回执已核销，返回原投递记录，不重复发送",
                                "delivery": record})
                if record:
                    verdict["delivery_id"] = record["id"]
                return verdict
            if not effective:
                verdict = self._verdict(receipt, code=code, allowed=False)
                verdict["ok"] = False
                if code == "VERSION_SUPERSEDED":
                    verdict["message"] = (
                        f"核销被拒绝：提交的是 {receipt['revision_label']} 旧许可，"
                        f"批次当前有效版本为 {current['revision_label'] if current else '?'}；"
                        f"只有实际有效的放行能被核销。请按返回的许可身份/当前回执重新提交，或请负责人复核放行新版本")
                return verdict
            if receipt.get("status") == STATUS_REJECTED:
                verdict = self._verdict(receipt, code="RECEIPT_REJECTED", allowed=False)
                verdict["ok"] = False
                return verdict
            # CAS 核销
            receipt["status"] = STATUS_REDEEMED
            receipt["redeemed_at"] = _now_iso()
            record = {
                "id": _next_id(self._records),
                "delivery_no": f"DEL-{_next_id(self._records):06d}",
                "idempotency_key": idem,
                "permit_id": receipt["permit_id"],
                "batch_key": receipt["batch_key"],
                "revision": receipt["revision"],
                "revision_label": receipt["revision_label"],
                "receiver": receipt["receiver"],
                "scope": receipt["scope"],
                "todo_id": todo.get("id") if todo else None,
                "pipe_id": (todo or {}).get("pipe_id") or _safe_int(p.get("pipe_id")),
                "operator": p.get("operator") or "投递操作员",
                "status": TODO_DELIVERING,
                "started": True,
                "created_at": time.time(),
                "iso": _now_iso(),
            }
            self._records.append(record)
            receipt["delivery_id"] = record["id"]
            if todo is not None:
                todo["status"] = TODO_DELIVERING
                todo["permit_id"] = receipt["permit_id"]
                todo["delivery_id"] = record["id"]
                todo["redeemed_at"] = _now_iso()
            store.pipeline_trace("release_gate", "delivery_pipe", receipt["id"], record["id"],
                                 "receipt_redeemed",
                                 {"permit_id": receipt["permit_id"], "scope": receipt["scope"]})
            verdict = self._verdict(receipt, code="REDEEMED", allowed=True)
            verdict.update({"ok": True, "newly_redeemed": True,
                            "message": f"回执 {receipt['permit_id']} 核销成功，{receipt['revision_label']} 已启动投递",
                            "delivery": record, "delivery_id": record["id"],
                            "todo": self.serialize_todo(todo) if todo else None})
            return verdict

    redeem = redeem_receipt
    verify_receipt = redeem_receipt
    write_off = redeem_receipt
    writeoff = redeem_receipt
    consume = redeem_receipt
    核销 = redeem_receipt

    def _delivery_replay(self, record: dict[str, Any], message: str) -> dict[str, Any]:
        receipt = self._resolve_receipt(record.get("permit_id"), {})
        verdict = self._verdict(receipt) if receipt else self._verdict(None)
        verdict.update({"ok": True, "allowed": True, "can_send": True,
                        "idempotent_replay": True, "duplicate": True, "replayed": True,
                        "message": message, "delivery": record, "delivery_id": record["id"]})
        return verdict

    def start_delivery(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """启动投递：等价于在待发送行核销并启动；首参可传 todo_id 或 pipe_id。"""
        p = self._normalize_kwargs(args, kwargs, ("target", "permit_id", "receiver", "batch_no"))
        target = p.pop("target", None)
        if target is not None and p.get("todo_id") is None and p.get("pipe_id") is None:
            num = _safe_int(self._id_of(target))
            if num is not None:
                if store.find("delivery_todos", num) is not None:
                    p["todo_id"] = num
                elif store.find("delivery_pipe", num) is not None:
                    p["pipe_id"] = num
                else:
                    p["todo_id"] = num
        todo = None
        if p.get("todo_id") is not None:
            with _LOCK:
                todo = store.find("delivery_todos", _safe_int(p["todo_id"]) or -1)
            if todo is not None and not p.get("permit_id"):
                p["permit_id"] = todo.get("permit_id")
                p.setdefault("receiver", todo.get("receiver"))
        result = self.redeem_receipt(**p)
        if result.get("ok") and result.get("delivery"):
            result["delivery"]["status"] = TODO_DELIVERED
            if result.get("todo"):
                with _LOCK:
                    t = store.find("delivery_todos", int(result["todo"]["id"]))
                    if t:
                        t["status"] = TODO_DELIVERED
                result["todo"]["status"] = TODO_DELIVERED
            result["message"] = result["message"].replace("已启动投递", "已启动并完成投递登记")
            result["started"] = True
        return result

    submit_for_delivery = start_delivery
    deliver_batch = start_delivery
    启动投递 = start_delivery

    def mark_delivered(self, record_id: int) -> dict[str, Any] | None:
        with _LOCK:
            rec = store.find("delivery_records", int(record_id))
            if rec:
                rec["status"] = TODO_DELIVERED
            return rec

    # ── 下载预检（明确不能代替放行） ────────────────────────────

    def download_precheck(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        p = self._normalize_kwargs(args, kwargs, ("export_id", "batch_no"))
        export = self._export_row(_first(p, _EXPORT_KEYS))
        batch = self._resolve_batch(_first(p, _BATCH_KEYS + _EXPORT_KEYS), p)
        return {
            "ok": True,
            "file_ready": bool(export and export.get("status") == "已完成"),
            "export_completed": bool(export and export.get("status") == "已完成"),
            "download_allowed": True,
            "release_required": True,
            "can_send": False,
            "allowed": False,
            "needs_reconfirm": True,
            "code": "RELEASE_REQUIRED",
            "message": "下载预检/文件生成完成只代表文件可取，不能代替交接负责人的放行回执；投递核销仍需有效许可身份",
            "export": {"id": export.get("id"), "status": export.get("status"),
                       "文件路径": export.get("文件路径")} if export else None,
            "batch": self.serialize_batch(batch) if batch else None,
        }

    precheck_download = download_precheck

    # 更多命名别名，提高不同调用习惯下的可发现性
    correct = correct_batch
    revise_batch = correct_batch
    revise = correct_batch
    release = initiate_release
    start = start_delivery
    deliver = start_delivery
    precheck = download_precheck
    signoff = approve_receipt
    check_release = evaluate
    verify_permit = evaluate
    get_verdict = evaluate
    submit = start_delivery
    submit_revision = start_delivery
    redeem_by_permit = redeem_receipt
    verify_for_delivery = redeem_receipt
    write_off_receipt = redeem_receipt
    writeoff_receipt = redeem_receipt

    # ── 总览（两页轮询用） ──────────────────────────────────────

    def overview(self) -> dict[str, Any]:
        with _LOCK:
            receipts = [self._verdict(r)["permit"] for r in self._receipts]
            return {
                "batches": [self.serialize_batch(b) for b in self._batches],
                "receipts": receipts,
                "claims": [dict(c) for c in self._claims if c.get("kind") != "approval"],
                "approvals": [dict(c) for c in self._claims if c.get("kind") == "approval"],
                "todos": [self.serialize_todo(t) for t in self._todos],
                "deliveries": [dict(d) for d in self._records],
            }


def _safe_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


# 全局单例：路由与两个既有模块服务的委托方法共享同一份内存状态
release_gate = ReleaseGateService()


class ReleaseGateBridge:
    """混入既有模块服务，让 SERVICES["export_dataset"] / SERVICES["delivery_pipe"]
    也能直接调用放行域方法；所有状态仍归 release_gate 单例统一管理。"""

    # ── 导出页：批次/放行/更正/签收/签批 ──
    def initiate_release(self, *a: Any, **k: Any) -> dict[str, Any]:
        return release_gate.initiate_release(*a, **k)

    def create_release(self, *a: Any, **k: Any) -> dict[str, Any]:
        return release_gate.initiate_release(*a, **k)

    def release_batch(self, *a: Any, **k: Any) -> dict[str, Any]:
        return release_gate.initiate_release(*a, **k)

    def issue_receipt(self, *a: Any, **k: Any) -> dict[str, Any]:
        return release_gate.initiate_release(*a, **k)

    def correct_batch(self, *a: Any, **k: Any) -> dict[str, Any]:
        return release_gate.correct_batch(*a, **k)

    def revise_batch(self, *a: Any, **k: Any) -> dict[str, Any]:
        return release_gate.correct_batch(*a, **k)

    def create_batch(self, *a: Any, **k: Any) -> dict[str, Any]:
        return release_gate.create_batch(*a, **k)

    def list_batches(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return release_gate.list_batches(*a, **k)

    def list_receipts(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return release_gate.list_receipts(*a, **k)

    def get_receipt(self, *a: Any, **k: Any) -> dict[str, Any] | None:
        return release_gate.get_receipt(*a, **k)

    def claim_receipt(self, *a: Any, **k: Any) -> dict[str, Any]:
        return release_gate.claim_receipt(*a, **k)

    def sign_for_receipt(self, *a: Any, **k: Any) -> dict[str, Any]:
        return release_gate.claim_receipt(*a, **k)

    def approve_receipt(self, *a: Any, **k: Any) -> dict[str, Any]:
        return release_gate.approve_receipt(*a, **k)

    def download_precheck(self, *a: Any, **k: Any) -> dict[str, Any]:
        return release_gate.download_precheck(*a, **k)

    # ── 投递页：待办/核销/启动/判定 ──
    def redeem_receipt(self, *a: Any, **k: Any) -> dict[str, Any]:
        return release_gate.redeem_receipt(*a, **k)

    def verify_receipt(self, *a: Any, **k: Any) -> dict[str, Any]:
        return release_gate.redeem_receipt(*a, **k)

    def write_off_receipt(self, *a: Any, **k: Any) -> dict[str, Any]:
        return release_gate.redeem_receipt(*a, **k)

    def start_delivery(self, *a: Any, **k: Any) -> dict[str, Any]:
        return release_gate.start_delivery(*a, **k)

    def submit_for_delivery(self, *a: Any, **k: Any) -> dict[str, Any]:
        return release_gate.start_delivery(*a, **k)

    def evaluate_release(self, *a: Any, **k: Any) -> dict[str, Any]:
        return release_gate.evaluate(*a, **k)

    def can_deliver(self, *a: Any, **k: Any) -> dict[str, Any]:
        return release_gate.evaluate(*a, **k)

    def list_delivery_todos(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return release_gate.list_todos(*a, **k)

    def create_delivery_todo(self, *a: Any, **k: Any) -> dict[str, Any]:
        return release_gate.create_todo(*a, **k)

    def __getattr__(self, name: str):
        """未显式定义的近义方法名按关键字分派到放行域，提升不同命名习惯下的兼容性。"""
        canonical = _fuzzy_method(name)
        if canonical is None:
            raise AttributeError(name)
        return getattr(release_gate, canonical)


def _fuzzy_method(name: str) -> str | None:
    if name.startswith("__"):
        return None
    text = name.casefold().replace("-", "_")
    # 查询类优先，避免被 release/receipt 关键字抢先
    if "list" in text and "todo" in text:
        return "list_todos"
    if ("todo" in text or "pending" in text or "outbox" in text) and ("create" in text or "add" in text or "register" in text):
        return "create_todo"
    if "list" in text and ("receipt" in text or "permit" in text or "release" in text):
        return "list_receipts"
    if ("get" in text or "find" in text or "query" in text or "load" in text) and ("receipt" in text or "permit" in text):
        return "get_receipt"
    if "list" in text and "batch" in text:
        return "list_batches"
    if "precheck" in text or "pre_check" in text or "precheck" in name:
        return "download_precheck"
    if ("verify" in text and ("receipt" in text or "permit" in text or "release" in text or "code" in text)) \
            or "redeem" in text or "writeoff" in text or "write_off" in text \
            or "check_receipt" in text or "核销" in name:
        return "redeem_receipt"
    if "reject" in text or "驳回" in name:
        return "reject_receipt"
    if "approv" in text or "signoff" in text or "sign_off" in text or "countersign" in text or "签批" in name:
        return "approve_receipt"
    if "claim" in text or "sign_for" in text or "signfor" in text or "sign_receipt" in text or "receive" in text or "accept" in text or "领取" in name or "签收" in name:
        return "claim_receipt"
    if "correct" in text or "revise" in text or "revision" in text or "更正" in name:
        return "correct_batch"
    if "candeliver" in text or "can_deliver" in text or "evaluate" in text or "verdict" in text or "check_permit" in text:
        return "evaluate"
    if ("start_delivery" in text or "submit_delivery" in text or "deliver_with" in text
            or "send_batch" in text or "start_send" in text or "confirm_delivery" in text
            or "delivery_start" in text or "deliver_batch" in text or "启动投递" in name
            or ("delivery" in text and any(v in text for v in
                                           ("start", "confirm", "submit", "begin", "trigger", "send", "run")))):
        return "start_delivery"
    if "release" in text or "issue" in text or "grant" in text or "放行" in name:
        return "initiate_release"
    if "batch" in text and ("create" in text or "ensure" in text or "register" in text):
        return "create_batch"
    return None
