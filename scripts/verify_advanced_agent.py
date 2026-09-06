#!/usr/bin/env python3
"""Profile-based verifier that never turns missing external evidence into PASS."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class Result:
    id: str
    status: str
    detail: str
    command: list[str] | None = None
    duration_seconds: float = 0
    output: str = ""


def command(
    check_id: str, argv: list[str], *,
    environment: dict[str, str] | None = None,
    unset: tuple[str, ...] = (),
) -> Result:
    started = time.monotonic()
    child_environment = os.environ.copy()
    for name in unset:
        child_environment.pop(name, None)
    child_environment.update(environment or {})
    try:
        completed = subprocess.run(  # noqa: S603 -- verifier only supplies repository-owned fixed argv
            argv, cwd=ROOT, capture_output=True, text=True, check=False, env=child_environment,
        )
    except OSError as exc:
        return Result(
            check_id, "BLOCKED", f"无法启动验证工具：{exc}", argv,
            round(time.monotonic() - started, 3),
        )
    output = ((completed.stdout or "") + (completed.stderr or ""))[-20_000:]
    summaries = re.findall(
        r"(?:^|\s)(\d+ passed(?:, \d+ (?:failed|skipped|deselected|warnings?))*(?: in [0-9.]+s)?)",
        output,
    )
    detail = f"exit={completed.returncode}"
    if summaries:
        detail += f"; {summaries[-1]}"
    return Result(
        check_id, "PASS" if completed.returncode == 0 else "FAIL",
        detail, argv, round(time.monotonic() - started, 3), output,
    )


def command_no_skips(
    check_id: str, argv: list[str], *,
    environment: dict[str, str] | None = None,
    unset: tuple[str, ...] = (),
) -> Result:
    result = command(check_id, argv, environment=environment, unset=unset)
    skipped = re.search(r"(?:^|[, ])(\d+) skipped(?:[, ]|$)", result.output)
    if result.status == "PASS" and skipped and int(skipped.group(1)):
        result.status = "BLOCKED"
        result.detail += "; 必需验收用例被跳过"
    return result


def database_integration() -> list[Result]:
    variables = ("MERIDIAN_TEST_POSTGRES_URL", "MERIDIAN_TEST_MYSQL_URL")
    missing = [name for name in variables if not os.getenv(name)]
    if missing:
        return [Result(
            "database-integration", "BLOCKED",
            f"真实 PostgreSQL/MySQL 验证未配置：{', '.join(missing)}",
        )]
    return [command_no_skips(
        "database-integration",
        [sys.executable, "-m", "pytest", "-q", "-m", "database_integration", "tests/test_database_integration.py"],
        environment={name: os.environ[name] for name in variables},
    )]


def root_compose_config() -> list[Result]:
    return [command(
        "production-compose-config", ["docker", "compose", "config", "-q"],
        environment={
            "MERIDIAN_SECRET_KEY": "verifier-session-key-at-least-32-characters",
            "MERIDIAN_ENCRYPTION_KEY": "verifier-encryption-key-at-least-32-characters",
            "MERIDIAN_BACKUP_KEY": "verifier-backup-key-at-least-32-characters",
            "MERIDIAN_BOOTSTRAP_TOKEN": "verifier-bootstrap-token-at-least-32-characters",
            "MERIDIAN_METRICS_TOKEN": "verifier-metrics-token-at-least-32-characters",
            "MERIDIAN_TRUSTED_HOSTS": "localhost",
            "MERIDIAN_OUTBOUND_HOST_ALLOWLIST": "api.openai.com",
            "MERIDIAN_SANDBOX_PROXY_TOKEN": "verifier-sandbox-token-at-least-32-characters",
        },
    )]


def probe(check_id: str, url: str, validator=None) -> Result:
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - operator-provided verifier endpoint
            payload = response.read(2 * 1024 * 1024)
            value = json.loads(payload) if payload else {}
        if validator and not validator(value):
            return Result(check_id, "FAIL", f"能力响应不满足要求：{url}", duration_seconds=round(time.monotonic() - started, 3))
        return Result(check_id, "PASS", url, duration_seconds=round(time.monotonic() - started, 3), output=json.dumps(value)[:4000])
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return Result(check_id, "BLOCKED", f"无法连接真实端点 {url}: {exc}", duration_seconds=round(time.monotonic() - started, 3))


def ci() -> list[Result]:
    test_environment = (
        "MERIDIAN_ENV", "MERIDIAN_SECRET_KEY", "MERIDIAN_ENCRYPTION_KEY", "MERIDIAN_BACKUP_KEY",
        "MERIDIAN_BOOTSTRAP_TOKEN", "MERIDIAN_METRICS_TOKEN",
        "MERIDIAN_TRUSTED_HOSTS", "MERIDIAN_ALLOWED_ORIGINS", "MERIDIAN_OUTBOUND_HOST_ALLOWLIST",
        "MERIDIAN_ALLOW_PRIVATE_NETWORK", "MERIDIAN_DATABASE_HOST_ALLOWLIST",
        "MERIDIAN_DATABASE_ALLOW_PRIVATE_NETWORK", "MERIDIAN_TEST_POSTGRES_URL", "MERIDIAN_TEST_MYSQL_URL",
    )
    return [
        command("python-compile", [
            sys.executable, "-m", "compileall", "-q", "backend", "scripts", "packaging", "deploy/sandbox", "app.py",
        ]),
        command("ruff", ["ruff", "check", "backend", "scripts", "packaging", "deploy/sandbox", "tests", "app.py"]),
        command("ruff-security", ["ruff", "check", "--select", "S", "backend", "scripts", "packaging", "deploy/sandbox", "app.py"]),
        command_no_skips("pytest", [
            sys.executable, "-m", "pytest", "-q", "-m", "not database_integration",
            "--cov=backend/agent", "--cov=backend/api", "--cov=backend/core", "--cov=backend/services",
            "--cov-report=term", "--cov-fail-under=60",
        ], unset=test_environment),
        command("agent-core-coverage", [
            sys.executable, "-m", "coverage", "report", "--include=backend/agent/*", "--fail-under=85",
        ]),
        *database_integration(),
        command("python-dependency-audit", ["pip-audit", "-r", "requirements.lock", "--no-deps", "--disable-pip"]),
        command("sandbox-proxy-dependency-audit", [
            "pip-audit", "-r", "deploy/sandbox/requirements-proxy.txt", "--no-deps", "--disable-pip",
        ]),
        command("frontend-check", ["npm", "run", "check"]),
        command("frontend-build", ["npm", "run", "build"]),
        command("frontend-dependency-audit", ["npm", "audit", "--audit-level=high"]),
        *root_compose_config(),
        command("warehouse-compose-config", ["docker", "compose", "-f", "deploy/warehouse/docker-compose.yml", "config", "-q"]),
        command("eval-contracts", [sys.executable, "scripts/validate_eval_contracts.py"]),
        *sandbox_integration(),
        *browser_integration(),
    ]


def repository_audit() -> list[Result]:
    return [
        command("repository-audit", [sys.executable, "scripts/audit_repository.py", "--check"]),
        command("audit-negative-fixture", [sys.executable, "scripts/audit_repository.py", "--self-test-negative"]),
    ]


def warehouse_reference() -> list[Result]:
    trino = os.getenv("MERIDIAN_VERIFY_TRINO_URL", "http://127.0.0.1:8080").rstrip("/")
    livy = os.getenv("MERIDIAN_VERIFY_LIVY_URL", "http://127.0.0.1:8998").rstrip("/")
    values = [
        probe("trino-cluster", f"{trino}/v1/info"),
        probe("trino-workers", f"{trino}/v1/node", lambda value: len(value) >= 3),
        probe("livy-batches", f"{livy}/batches?from=0&size=1", lambda value: isinstance(value.get("sessions"), list)),
    ]
    evidence = os.getenv("MERIDIAN_WAREHOUSE_EVIDENCE")
    if not evidence:
        values.append(Result(
            "warehouse-e2e-evidence", "BLOCKED",
            "MERIDIAN_WAREHOUSE_EVIDENCE 未指定；不能证明 Iceberg 物化、2 workers/2 executors、取消恢复与四图邮件链路",
        ))
    else:
        path = Path(evidence)
        if not path.is_file():
            values.append(Result("warehouse-e2e-evidence", "BLOCKED", f"证据文件不存在：{path}"))
        else:
            try:
                payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
                required = {
                    "reference_version", "trino_worker_count", "spark_executor_count", "iceberg_snapshot",
                    "spark_manifest", "cancel_recovery", "native_limits", "authorization",
                    "notification_received", "chart_count",
                }
                missing = sorted(required - set(payload))
                valid = (
                    not missing and int(payload.get("trino_worker_count", 0)) >= 2
                    and int(payload.get("spark_executor_count", 0)) >= 2
                    and int(payload.get("chart_count", 0)) == 4
                    and all(bool(payload.get(key)) for key in (
                        "iceberg_snapshot", "spark_manifest", "cancel_recovery", "native_limits",
                        "authorization", "notification_received",
                    ))
                )
                values.append(Result(
                    "warehouse-e2e-evidence", "PASS" if valid else "FAIL",
                    "真实参考环境证据已校验" if valid else f"证据缺失或不足：{missing}", output=json.dumps(payload)[:8000],
                ))
            except (OSError, ValueError, TypeError) as exc:
                values.append(Result("warehouse-e2e-evidence", "FAIL", f"证据无效：{exc}"))
    return values


def target_platform() -> list[Result]:
    return json_evidence(
        "target-platform", "MERIDIAN_TARGET_PLATFORM_EVIDENCE",
        {
            "platform", "engine", "version", "identity", "authorization", "query",
            "materialization", "cancellation", "recovery", "controlled_load",
        },
        lambda value: all(value.get(key) for key in (
            "platform", "engine", "version", "identity", "authorization", "query",
            "materialization", "cancellation", "recovery", "controlled_load",
        )),
        missing_detail="不会用参考 Trino 自动替代客户目标平台",
    )


def scale() -> list[Result]:
    numeric = ("dataset_bytes", "scanned_bytes", "processed_rows", "concurrency", "p50_seconds", "p95_seconds")
    return json_evidence(
        "scale", "MERIDIAN_SCALE_EVIDENCE",
        {"authorized_scope", "dataset_bytes", "scanned_bytes", "processed_rows", "cluster", "concurrency",
         "cache_modes", "p50_seconds", "p95_seconds", "failure_recovery", "cost"},
        lambda value: value.get("authorized_scope") is True
        and all(isinstance(value.get(key), (int, float)) and value[key] > 0 for key in numeric)
        and isinstance(value.get("cache_modes"), list) and {"cold", "warm"}.issubset(set(value["cache_modes"]))
        and bool(value.get("cluster")) and bool(value.get("failure_recovery")) and value.get("cost") is not None,
        missing_detail="不以合成字节字段冒充 PB/EB 实测",
    )


def json_evidence(
    name: str,
    variable: str,
    required: set[str],
    validator,
    *,
    missing_detail: str = "必须由真实外部环境产生",
) -> list[Result]:
    evidence = os.getenv(variable)
    if not evidence:
        return [Result(name, "BLOCKED", f"{variable} 未配置；{missing_detail}")]
    path = Path(evidence)
    if not path.is_file():
        return [Result(name, "BLOCKED", f"证据文件不存在：{path}")]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("证据顶层必须是 JSON 对象")
        missing = sorted(required - set(payload))
        valid = not missing and bool(validator(payload))
        return [Result(
            name, "PASS" if valid else "FAIL",
            f"已校验真实证据：{path}" if valid else f"证据缺失或未达门槛：{missing}",
            output=json.dumps(payload, ensure_ascii=False)[:8000],
        )]
    except (OSError, ValueError, TypeError) as exc:
        return [Result(name, "FAIL", f"证据无效：{exc}")]


def live_model() -> list[Result]:
    def valid(value: dict[str, Any]) -> bool:
        runs = value.get("runs")
        if not isinstance(runs, list) or not runs:
            return False
        counts: dict[str, int] = {}
        for run in runs:
            if not isinstance(run, dict) or not run.get("task_id"):
                return False
            counts[str(run["task_id"])] = counts.get(str(run["task_id"]), 0) + 1
            if run.get("safety_pass") is not True or run.get("published_value_match") is False:
                return False
        repeated = [count for count in counts.values() if count >= 3]
        success_rate = sum(run.get("status") == "complete" for run in runs) / len(runs)
        threshold = value.get("thresholds", {}).get("complete_success_rate")
        return isinstance(threshold, (int, float)) and len(repeated) >= 12 and success_rate >= float(threshold)

    return json_evidence(
        "live-model", "MERIDIAN_LIVE_MODEL_EVIDENCE",
        {"provider", "model", "prompt_version", "tool_version", "skill_versions", "thresholds", "runs"},
        lambda value: isinstance(value.get("thresholds"), dict)
        and float(value["thresholds"].get("complete_success_rate", 2)) >= 0.9 and valid(value),
        missing_detail="必须对至少 12 个任务各运行 3 次真实模型评估",
    )


def notification() -> list[Result]:
    return json_evidence(
        "notification", "MERIDIAN_NOTIFICATION_EVIDENCE",
        {"transport", "recipient", "message_id", "received", "attachment_hashes"},
        lambda value: value.get("received") is True and bool(value.get("message_id"))
        and isinstance(value.get("attachment_hashes"), list) and bool(value["attachment_hashes"]),
    )


def migration_restore() -> list[Result]:
    return json_evidence(
        "migration-restore", "MERIDIAN_MIGRATION_EVIDENCE",
        {"backup_sha256", "source_counts", "restored_counts", "integrity_pass", "restore_pass", "rollback_tested"},
        lambda value: value.get("integrity_pass") is True and value.get("restore_pass") is True
        and value.get("rollback_tested") is True and value.get("source_counts") == value.get("restored_counts"),
    )


def sandbox_integration() -> list[Result]:
    from backend.services.data_plane.sandbox import SandboxRunner

    image = os.getenv("MERIDIAN_SANDBOX_IMAGE", "meridian-sandbox:py311-20260906")
    with tempfile.TemporaryDirectory(prefix="meridian-sandbox-verify-") as raw:
        root = Path(raw)
        runner = SandboxRunner(image=image, input_root=root / "input", output_root=root / "output")
        capability = runner.capability()
        if not capability["available"]:
            return [Result("sandbox-integration", "BLOCKED", str(capability.get("error") or "Docker daemon 不可用"))]
        inspected = command("sandbox-image", ["docker", "image", "inspect", image])
        if inspected.status != "PASS":
            inspected.status = "BLOCKED"
            inspected.detail = f"固定 sandbox 镜像未构建：{image}"
            return [inspected]
        (root / "input").mkdir()
        (root / "output").mkdir()
        (root / "input" / "input.csv").write_text("group,value\na,1\na,2\nb,3\n", encoding="utf-8")
        try:
            result = runner.execute(
                {"input": "input.csv", "method": "describe", "code": None, "parameters": {}},
                input_dir=root / "input", run_id="verification",
            )
            valid = result.get("status") == "SUCCEEDED" and any(
                item.get("path") == "result.parquet" for item in result.get("files") or []
            )
            return [Result(
                "sandbox-integration", "PASS" if valid else "FAIL",
                "真实无网容器作业已执行" if valid else "sandbox 未生成声明的 Parquet 结果",
                output=json.dumps(result, ensure_ascii=False)[:4000],
            )]
        except Exception as exc:
            return [Result("sandbox-integration", "FAIL", f"sandbox 真实作业失败：{exc}")]


def browser_integration() -> list[Result]:
    package = ROOT / "node_modules" / "@playwright" / "test" / "package.json"
    if not package.is_file():
        return [Result("browser-integration", "BLOCKED", "Playwright 固定依赖未安装")]
    return [command("browser-integration", ["npm", "run", "test:browser"])]


def acceptance_matrix() -> list[Result]:
    path = ROOT / "docs" / "advanced-data-agent" / "ACCEPTANCE.md"
    if not path.is_file():
        return [Result("acceptance-matrix", "FAIL", "验收矩阵不存在")]
    rows = re.findall(
        r"^\| ((?:R|F|A|W|C)\d{2}) \| ([A-Z_]+) \| ([A-Z_]+) \|",
        path.read_text(encoding="utf-8"), re.MULTILINE,
    )
    expected = {**{f"R{i:02d}": 1 for i in range(1, 22)}, **{f"F{i:02d}": 1 for i in range(1, 35)},
                **{f"A{i:02d}": 1 for i in range(1, 65)}, **{f"W{i:02d}": 1 for i in range(1, 31)},
                **{f"C{i:02d}": 1 for i in range(1, 25)}}
    counts = {key: sum(item[0] == key for item in rows) for key in expected}
    malformed = [key for key, count in counts.items() if count != 1]
    structure = Result(
        "acceptance-matrix-structure", "PASS" if not malformed and len(rows) == len(expected) else "FAIL",
        f"已解析 {len(rows)} / {len(expected)} 个唯一验收项；异常：{malformed[:20]}",
    )
    implementation_statuses = {implementation for _, implementation, _ in rows}
    validation_statuses = {validation for _, _, validation in rows}
    invalid_implementation = implementation_statuses - {"IMPLEMENTED", "RETIRED"}
    gate_status = (
        "FAIL" if malformed or invalid_implementation or "FAIL" in validation_statuses
        else "PASS" if validation_statuses == {"PASS"}
        else "BLOCKED"
    )
    gate = Result(
        "acceptance-matrix-gates", gate_status,
        "全部验收项已通过" if gate_status == "PASS" else (
            f"实现状态={sorted(implementation_statuses)}；验证状态={sorted(validation_statuses)}"
        ),
    )
    return [structure, gate]


def execute(profile: str) -> list[Result]:
    if profile == "ci":
        return ci()
    if profile == "repository-audit":
        return repository_audit()
    if profile == "warehouse-reference":
        return warehouse_reference()
    if profile == "target-platform":
        return target_platform()
    if profile == "scale":
        return scale()
    if profile == "live-model":
        return live_model()
    if profile == "notification":
        return notification()
    if profile == "migration-restore":
        return migration_restore()
    if profile == "release":
        return [
            *ci(), *repository_audit(), *warehouse_reference(), *target_platform(),
            *scale(), *live_model(), *notification(), *migration_restore(), *acceptance_matrix(),
        ]
    raise ValueError(profile)


def write_report(profile: str, results: list[Result], output_dir: Path) -> dict:
    status = "FAIL" if any(item.status == "FAIL" for item in results) else "BLOCKED" if any(item.status == "BLOCKED" for item in results) else "PASS"
    payload = {"schema_version": 1, "profile": profile, "status": status, "results": [asdict(item) for item in results]}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{profile}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows = [f"# Verification: {profile}", "", f"Overall: **{status}**", "", "| Check | Status | Detail |", "|---|---|---|"]
    rows.extend(f"| {item.id} | {item.status} | {item.detail.replace('|', '/')} |" for item in results)
    (output_dir / f"{profile}.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True, choices=(
        "ci", "repository-audit", "warehouse-reference", "target-platform", "scale",
        "live-model", "notification", "migration-restore", "release",
    ))
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/verification")
    args = parser.parse_args()
    results = execute(args.profile)
    payload = write_report(args.profile, results, args.output_dir)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
