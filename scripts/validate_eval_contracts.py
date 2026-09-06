#!/usr/bin/env python3
"""Validate eval inventory structure without pretending to execute external tasks."""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    task_path = ROOT / "evals/advanced_agent/tasks.jsonl"
    gold_path = ROOT / "evals/advanced_agent/gold.json"
    tasks = [json.loads(line) for line in task_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [str(item.get("id") or "") for item in tasks]
    if len(tasks) < 30 or len(ids) != len(set(ids)) or any(not value for value in ids):
        raise SystemExit("eval tasks 必须至少30条且ID唯一非空")
    required = {"id", "category", "prompt", "expected_tools", "expected", "external_required"}
    invalid = [item.get("id") for item in tasks if required - set(item)]
    if invalid:
        raise SystemExit("eval tasks 缺字段：" + ", ".join(map(str, invalid)))
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    if not gold.get("hard_failures") or gold.get("minimum", {}).get("blocked_is_not_pass") is not True:
        raise SystemExit("gold 必须定义硬失败且 BLOCKED 不等于 PASS")
    print(json.dumps({
        "status": "PASS", "task_count": len(tasks),
        "external_count": sum(bool(item["external_required"]) for item in tasks),
        "note": "仅验证评估合同结构，未把任务标为已执行",
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
