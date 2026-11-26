"""路由汇总：放行回执专用路由先注册（含固定/多段路径），通用资源路由最后兜底。"""
from __future__ import annotations

from app.routers import release, resource

ROUTERS = [release, resource]
