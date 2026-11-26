"""投递管道领域服务：采集数据下游投递管道配置、传输监控与重试机制。

跨模块数据流：
- delivery_pipe ← robot_verify: 反封锁检测信息同步到投递管道
"""
from __future__ import annotations

import random
import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.release_gate import ReleaseGateBridge, release_gate
from app.store import store


class DeliveryPipeService(ReleaseGateBridge, BaseModuleService):
    MODULE = "delivery_pipe"
    ENTITY = "投递配置"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["配置编号", "目标系统", "投递方式", "接口地址", "数据筛选", "投递频率", "重试次数", "最后投递"]
    REQUIRED_FIELDS = ["配置编号", "目标系统", "投递方式"]
    STATUS_ORDER = ["草稿", "待验证", "运行中", "已停用"]
    ACTION_RULES = {"提交验证": "待验证", "启动管道": "运行中", "停用管道": "已停用"}
    NEGATIVE_ACTIONS = ["停用管道"]

    TARGET_SYSTEMS = ["Kafka", "RabbitMQ", "HTTP_API", "FTP", "S3", "HDFS", "Elasticsearch",
                      "Database", "Redis_Stream", "Webhook"]
    DELIVERY_METHODS = ["push", "pull", "batch", "stream", "event_driven"]

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_delivery_history"] = []
        entry["_total_deliveries"] = 0
        entry["_successful_deliveries"] = 0
        entry["_failed_deliveries"] = 0
        entry["_retry_attempts"] = 0
        store.record_metric("delivery_pipe", "pipe_created", 1,
                            {"target": values.get("目标系统", ""),
                             "method": values.get("投递方式", "")})
        return entry, []

    def deliver(self, entry_id: int, data: list[dict[str, Any]], auto_retry: bool = True,
                **release_kwargs: Any) -> dict[str, Any]:
        """执行投递操作，支持自动重试。

        若传入 permit_id/receipt/批次版本/receiver 等放行上下文，或显式
        enforce_release=True，则改走放行回执核销状态机：没有当前有效许可身份
        一律不能启动投递。不带这些参数时保留原有模拟投递行为（兼容旧调用）。
        """
        gated_keys = ("permit_id", "receipt_id", "receipt", "permit", "batch_key",
                      "batch_no", "revision", "enforce_release")
        if release_kwargs.get("enforce_release") or any(
                release_kwargs.get(k) not in (None, "") for k in gated_keys if k != "enforce_release"):
            release_kwargs.setdefault("pipe_id", entry_id)
            verdict = release_gate.start_delivery(**release_kwargs)
            verdict["status"] = "success" if verdict.get("ok") else "blocked"
            return verdict
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"status": "error", "reason": "配置不存在"}
        if entry.get("status") != "运行中":
            return {"status": "error", "reason": "管道未运行"}
        entry["_total_deliveries"] += 1
        max_retries = int(entry.get("重试次数", "3"))
        for attempt in range(1, max_retries + 2):
            success = random.random() > 0.2
            if success:
                entry["_successful_deliveries"] += 1
                entry["最后投递"] = time.strftime("%Y-%m-%d %H:%M:%S")
                record = {"ts": time.time(), "batch_size": len(data) if data else 0,
                          "success": True, "attempt": attempt}
                entry["_delivery_history"].append(record)
                store.record_metric("delivery_pipe", "delivery_success", len(data) if data else 0,
                                    {"target": entry.get("目标系统", ""),
                                     "attempt": str(attempt)})
                return {"status": "success", "delivered": len(data) if data else 0,
                        "attempts": attempt}
            if not auto_retry or attempt > max_retries:
                break
            entry["_retry_attempts"] += 1
            time.sleep(random.uniform(0.5, 2.0))
        entry["_failed_deliveries"] += 1
        store.record_metric("delivery_pipe", "delivery_failed", 1,
                            {"target": entry.get("目标系统", ""),
                             "attempts": str(max_retries + 1)})
        return {"status": "failed", "reason": "超过重试次数", "attempts": max_retries + 1}

    def receive_detection_sync(self, entry_id: int, robot_verify_id: int,
                               detection_data: dict[str, Any]) -> dict[str, Any] | None:
        """接收来自机器人校验的检测信息。跨模块：delivery_pipe ← robot_verify"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        store.pipeline_trace(
            "robot_verify", "delivery_pipe", robot_verify_id, entry_id,
            "receive_detection", {"blocked": detection_data.get("blocked", False)}
        )
        # 投递检测事件
        return self.deliver(entry_id, [detection_data], auto_retry=False)

    def topic_overview(self, entry_id: int) -> dict[str, Any]:
        """管道状态总览。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"error": "不存在"}
        return {
            "total": entry["_total_deliveries"],
            "success": entry["_successful_deliveries"],
            "failed": entry["_failed_deliveries"],
            "retries": entry["_retry_attempts"],
            "success_rate": (
                entry["_successful_deliveries"] / max(entry["_total_deliveries"], 1)
            ),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        pipes = store.rows(self.MODULE)
        base["running_pipes"] = sum(1 for p in pipes if p.get("status") == "运行中")
        base["total_deliveries"] = sum(p.get("_total_deliveries", 0) for p in pipes)
        base["overall_success_rate"] = (
            sum(p.get("_successful_deliveries", 0) for p in pipes) /
            max(sum(p.get("_total_deliveries", 0) for p in pipes), 1)
        )
        return base