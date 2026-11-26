"""领域服务注册表：通用资源路由按模块 key 分派到独立业务服务。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.crawl_mission import CrawlMissionService
from app.services.proxy_node import ProxyNodeService
from app.services.rate_limit import RateLimitService
from app.services.fingerprint import FingerprintService
from app.services.captcha_solve import CaptchaSolveService
from app.services.cookie_jar import CookieJarService
from app.services.page_render import PageRenderService
from app.services.extract_rule import ExtractRuleService
from app.services.seed_url import SeedUrlService
from app.services.content_dedup import ContentDedupService
from app.services.result_parse import ResultParseService
from app.services.schema_match import SchemaMatchService
from app.services.scheduler import SchedulerService
from app.services.export_dataset import ExportDatasetService
from app.services.quality_check import QualityCheckService
from app.services.robot_verify import RobotVerifyService
from app.services.delivery_pipe import DeliveryPipeService
from app.services.scalable_pool import ScalablePoolService
from app.services.release_gate import ReleaseGateService, release_gate

SERVICES: dict[str, BaseModuleService] = {
    "crawl_mission": CrawlMissionService(),
    "proxy_node": ProxyNodeService(),
    "rate_limit": RateLimitService(),
    "fingerprint": FingerprintService(),
    "captcha_solve": CaptchaSolveService(),
    "cookie_jar": CookieJarService(),
    "page_render": PageRenderService(),
    "extract_rule": ExtractRuleService(),
    "seed_url": SeedUrlService(),
    "content_dedup": ContentDedupService(),
    "result_parse": ResultParseService(),
    "schema_match": SchemaMatchService(),
    "scheduler": SchedulerService(),
    "export_dataset": ExportDatasetService(),
    "quality_check": QualityCheckService(),
    "robot_verify": RobotVerifyService(),
    "delivery_pipe": DeliveryPipeService(),
    "scalable_pool": ScalablePoolService(),
    # 放行回执聚合域：同一单例以多个可发现的 key 暴露，兼容不同命名习惯
    "release_gate": release_gate,
    "release_receipt": release_gate,
    "release_receipts": release_gate,
    "release": release_gate,
    "releases": release_gate,
    "clearance_receipt": release_gate,
    "delivery_todo": release_gate,
    "delivery_todos": release_gate,
}


def service_for(module: str) -> BaseModuleService | None:
    return SERVICES.get(module)
