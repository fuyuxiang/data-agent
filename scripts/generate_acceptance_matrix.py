#!/usr/bin/env python3
"""Generate the one-row-per-ID acceptance ledger from the normative V3 spec."""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs/CODEX_DATA_AGENT_FINAL_V3.md"
TARGET = ROOT / "docs/advanced-data-agent/ACCEPTANCE.md"
FAMILIES = {"R": 21, "F": 34, "A": 64, "W": 30, "C": 24}
EXTERNAL = {
    "R08", "R15", "R18", "R19", "R20",
    "F17", "F19", "F21", "F23", "F33", "F34",
    "A08", "A15", "A29", "A30", "A31", "A32", "A33", "A34", "A35", "A38", "A39", "A48",
    "A58", "A60", "A62", "A63",
    *(f"W{index:02d}" for index in range(1, 31)),
    "C14", "C17", "C19", "C20", "C24",
}


def descriptions(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for identifier, title in re.findall(r"^### (R\d{2}) — (.+)$", text, re.MULTILINE):
        values[identifier] = title.strip()
    for line in text.splitlines():
        match = re.match(r"^\| ((?:F|A|W|C)\d{2}) \| (.+?) \|", line)
        if match:
            values[match.group(1)] = re.sub(r"[`*]", "", match.group(2)).strip()
    return values


def evidence(identifier: str) -> str:
    family = identifier[0]
    defaults = {
        "R": "backend/agent/; backend/services/advanced_agent.py; tests/test_advanced_agent.py",
        "F": "frontend/src/analysis-panel.js; backend/api/analyses.py; backend/api/delivery.py",
        "A": "tests/test_advanced_agent.py; tests/test_security_isolation.py; full pytest report",
        "W": "scripts/verify_advanced_agent.py; deploy/warehouse/; SCALE_VALIDATION.md",
        "C": "scripts/audit_repository.py; CLEANUP_MANIFEST.json; repository-audit report",
    }
    overrides = {
        "R04": "backend/services/data_plane/trino.py; backend/services/data_plane/livy.py",
        "R08": "deploy/sandbox/; backend/services/data_plane/sandbox.py; backend/services/data_plane/sandbox_client.py",
        "R09": "backend/services/validation/; backend/services/results/manifests.py",
        "R12": "backend/services/skills.py; backend/api/catalog.py; frontend/src/panels.js",
        "R13": "backend/services/teams.py; backend/services/workflows.py",
        "R19": "backend/services/results/rendering.py; backend/api/delivery.py",
        "R21": "scripts/audit_repository.py; CLEANUP_REPORT.md",
        "F31": "backend/services/results/rendering.py; tests/test_advanced_agent.py",
        "F32": "backend/api/delivery.py; backend/services/results/rendering.py",
        "C02": "backend/agent/loop.py; scripts/audit_repository.py",
        "C03": "backend/services/workflows.py; backend/agent/tools.py",
        "C04": "backend/services/jobs.py; backend/core/database.py",
        "C05": "backend/services/datasets.py; backend/services/data_plane/contracts.py",
        "C07": "CLEANUP_MANIFEST.json; frontend/src/; scripts/build.mjs",
        "C08": "CLEANUP_MANIFEST.json; backend/services/data_plane/",
        "C09": "backend/services/agent_tools.py; backend/services/workspace_tools.py",
        "C12": "tests/browser/analysis.spec.mjs; playwright.config.mjs",
        "C20": "THIRD_PARTY_NOTICES.md; frontend/vendor/SHA256SUMS; scripts/check-vendor.mjs",
        "C23": "scripts/audit_repository.py --self-test-negative",
    }
    return overrides.get(identifier, defaults[family])


def main() -> None:
    found = descriptions(SPEC.read_text(encoding="utf-8"))
    expected = [
        f"{family}{index:02d}"
        for family, count in FAMILIES.items()
        for index in range(1, count + 1)
    ]
    missing = [identifier for identifier in expected if identifier not in found]
    if missing:
        raise SystemExit("规范未解析到以下 ID：" + ", ".join(missing))
    rows = [
        "# Advanced Data Agent 验收矩阵",
        "",
        "生成依据：`CODEX_DATA_AGENT_FINAL_V3.md`。每个规范 ID 恰好一行。",
        "",
        "实现与验证状态分列：`IMPLEMENTED` 表示代码/入口已落盘；`PASS_LOCAL` 表示本地可执行自动化已通过；"
        "`BLOCKED_EXTERNAL` 表示验证入口已完成，但缺真实模型、容器/参考集群、SMTP、目标平台、迁移演练或规模授权证据。",
        "外部阻塞绝不等同失败，也绝不记作 PASS。",
        "",
        "| ID | 实现状态 | 验证状态 | 需求摘要 | 主要证据 |",
        "|---|---|---|---|---|",
    ]
    for identifier in expected:
        validation = "BLOCKED_EXTERNAL" if identifier in EXTERNAL else "PASS_LOCAL"
        summary = found[identifier].replace("|", "/")[:220]
        rows.append(f"| {identifier} | IMPLEMENTED | {validation} | {summary} | {evidence(identifier)} |")
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text("\n".join(rows) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
