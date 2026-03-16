"""
查询规划共享类型
"""

from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class QueryPlan:
    """统一的查询计划结构。"""

    intent: str
    sql: str
    params: List[Any]
    filters: Dict[str, Any]
