from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .contracts import BoundedTransferPolicy, DatasetRef


class ExecutionRoute(str, Enum):
    WAREHOUSE_SQL = "WAREHOUSE_SQL"
    REMOTE_SPARK = "REMOTE_SPARK"
    BOUNDED_SANDBOX = "BOUNDED_SANDBOX"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class RouteDecision:
    route: ExecutionRoute
    reason: str


class ExecutionRouter:
    def __init__(self, bounded_policy: BoundedTransferPolicy | None = None):
        self.bounded_policy = bounded_policy or BoundedTransferPolicy()

    def choose(
        self,
        refs: list[DatasetRef],
        *,
        operation: str,
        warehouse_available: bool,
        spark_available: bool,
        common_remote_domain: bool = False,
        run_egress_bytes: int = 0,
    ) -> RouteDecision:
        remote = [ref for ref in refs if ref.kind in {"logical_relation", "remote_table", "remote_objects"}]
        heavy = operation in {"large_join", "feature_build", "distributed_ml", "grouped_anomaly"}
        if remote and heavy:
            if spark_available and (len(refs) == 1 or common_remote_domain):
                return RouteDecision(ExecutionRoute.REMOTE_SPARK, "重型计算在授权分布式数据域执行")
            if warehouse_available and operation in {"large_join", "feature_build", "grouped_anomaly"} and common_remote_domain:
                return RouteDecision(ExecutionRoute.WAREHOUSE_SQL, "计算可由共同仓侧引擎完成")
            return RouteDecision(ExecutionRoute.BLOCKED, "远端引用缺少可授权的共同计算域")
        if remote and warehouse_available and operation in {"filter", "project", "aggregate", "window", "validate"}:
            return RouteDecision(ExecutionRoute.WAREHOUSE_SQL, "关系计算优先下推到数仓")
        try:
            for ref in refs:
                self.bounded_policy.approve(ref, run_egress_bytes=run_egress_bytes)
        except ValueError as exc:
            return RouteDecision(ExecutionRoute.BLOCKED, str(exc))
        return RouteDecision(ExecutionRoute.BOUNDED_SANDBOX, "全部输入通过本地完整数据门禁")
