"""数据导出领域服务：采集数据批量导出、格式转换与交付路径。

跨模块数据流：
- export_dataset ← quality_check: 质量抽检通过后触发导出
"""
from __future__ import annotations

import json
import random
import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.release_gate import ReleaseGateBridge
from app.store import store


class ExportDatasetService(ReleaseGateBridge, BaseModuleService):
    MODULE = "export_dataset"
    ENTITY = "导出任务"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["任务编号", "数据来源", "导出格式", "字段筛选", "行数范围", "压缩方式", "文件路径", "导出时间"]
    REQUIRED_FIELDS = ["任务编号", "数据来源", "导出格式"]
    STATUS_ORDER = ["待执行", "导出中", "已完成", "已失败"]
    ACTION_RULES = {"发起导出": "导出中", "标记完成": "已完成", "标记失败": "已失败"}

    SUPPORTED_FORMATS = ["CSV", "JSON", "Parquet", "Excel", "XML", "Avro"]
    COMPRESSION = ["none", "gzip", "snappy", "lz4", "zstd"]

    def create_entry(self, values: dict[str, Any]) -> tuple:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["_export_progress"] = 0.0
        entry["_exported_rows"] = 0
        entry["_export_errors"] = []
        entry["_start_ts"] = 0
        store.record_metric("export_dataset", "export_task_created", 1,
                            {"format": values.get("导出格式", ""),
                             "source": values.get("数据来源", "")})
        return entry, []

    def execute_export(self, entry_id: int, data: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """执行数据导出。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        entry["status"] = "导出中"
        entry["_start_ts"] = time.time()
        fmt = entry.get("导出格式", "CSV")
        if fmt not in self.SUPPORTED_FORMATS:
            entry["status"] = "已失败"
            entry["_export_errors"].append(f"不支持的格式：{fmt}")
            entry["abnormal"] = True
            return entry
        # 模拟导出
        self._simulate_export()
        row_count = data if isinstance(data, int) else random.randint(100, 100000)
        entry["_exported_rows"] = row_count if isinstance(row_count, int) else len(data) if data else 0
        entry["行数范围"] = f"1-{entry['_exported_rows']}"
        entry["文件路径"] = f"/exports/{entry.get('任务编号', 'TASK')}.{fmt.lower()}"
        compression = entry.get("压缩方式", "none")
        if compression != "none":
            entry["文件路径"] += f".{compression}"
        entry["导出时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
        entry["status"] = "已完成"
        entry["pending"] = False
        entry["_export_progress"] = 100.0
        store.record_metric("export_dataset", "export_completed", entry["_exported_rows"],
                            {"format": fmt, "compression": compression})
        return entry

    def export_with_quality_check(self, entry_id: int, quality_check_id: int,
                                  passed: bool) -> dict[str, Any] | None:
        """质量抽检通过后触发导出。跨模块：quality_check → export_dataset"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        if not passed:
            entry["status"] = "已失败"
            entry["abnormal"] = True
            return entry
        store.pipeline_trace(
            "quality_check", "export_dataset", quality_check_id, entry_id,
            "quality_passed", {"action": "trigger_export"}
        )
        return self.execute_export(entry_id)

    def format_to_bytes(self, entry_id: int) -> dict[str, Any] | None:
        """模拟格式转换，返回文件大小。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        sizes = {"CSV": 1.2, "JSON": 1.5, "Parquet": 0.6, "Excel": 2.0, "XML": 2.5, "Avro": 0.4}
        row_factor = entry.get("_exported_rows", 1000) / 1000
        size_mb = sizes.get(entry.get("导出格式", "CSV"), 1.0) * row_factor
        return {"format": entry.get("导出格式"), "rows": entry.get("_exported_rows"),
                "size_mb": round(size_mb, 2), "compression": entry.get("压缩方式", "none")}

    def _simulate_export(self) -> None:
        time.sleep(random.uniform(0.05, 0.2))

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        tasks = store.rows(self.MODULE)
        base["completed_exports"] = sum(1 for t in tasks if t.get("status") == "已完成")
        base["total_rows_exported"] = sum(t.get("_exported_rows", 0) for t in tasks)
        return base