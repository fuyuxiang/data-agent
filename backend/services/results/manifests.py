from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ...agent.store import RunStore
from ...core.database import Database, utcnow
from ..validation.engine import Rule, ValidationEngine, outcome
from .rendering import build_manifest_payload


NUMBER_RE = re.compile(r"(?<![\w.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?")


def _result_for_ref(database: Database, workspace_id: str, ref_id: str) -> dict | None:
    result = database.get("query_results", ref_id, workspace_id=workspace_id)
    if result:
        return result
    with database.connect() as connection:
        row = connection.execute(
            "SELECT payload FROM dataset_refs WHERE id=? AND workspace_id=?", (ref_id, workspace_id),
        ).fetchone()
    if not row:
        return None
    payload = json.loads(row["payload"])
    result_id = str((payload.get("location") or {}).get("query_result_id") or "")
    return database.get("query_results", result_id, workspace_id=workspace_id) if result_id else None


def _expand_result_refs(
    database: Database, workspace_id: str, ref_id: str, seen: set[str] | None = None,
) -> list[str]:
    """Resolve a validated child publication to its immutable data evidence."""
    seen = seen or set()
    if not ref_id or ref_id in seen:
        return []
    seen.add(ref_id)
    if _result_for_ref(database, workspace_id, ref_id):
        return [ref_id]
    with database.connect() as connection:
        row = connection.execute(
            "SELECT manifest_id FROM publications WHERE id=? AND workspace_id=?",
            (ref_id, workspace_id),
        ).fetchone()
        manifest = connection.execute(
            "SELECT payload FROM result_manifests WHERE id=? AND workspace_id=?",
            (row["manifest_id"], workspace_id),
        ).fetchone() if row else None
    if not manifest:
        return []
    payload = json.loads(manifest["payload"])
    expanded = []
    for child_ref in payload.get("evidence_refs") or []:
        expanded.extend(_expand_result_refs(database, workspace_id, str(child_ref), seen))
    return list(dict.fromkeys(expanded))


def _evidence_cells(
    database: Database, workspace_id: str, refs: list[str], max_cells: int = 200_000,
) -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = []
    seen_results: set[str] = set()
    for ref_id in refs:
        result = _result_for_ref(database, workspace_id, ref_id)
        if not result or result["id"] in seen_results or result.get("completeness") != "complete":
            continue
        seen_results.add(result["id"])
        path = Path(str(result.get("path") or ""))
        frame = pd.read_csv(path) if path.is_file() else pd.DataFrame(result.get("data") or [])
        for row_index, row in frame.iterrows():
            for column, value in row.items():
                if len(cells) >= max_cells:
                    return cells
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(numeric):
                    continue
                cells.append({
                    "ref": f"{result['id']}[row={int(row_index)},column={column}]",
                    "result_id": result["id"], "row": int(row_index),
                    "column": str(column), "value": numeric,
                    "semantic_query": result.get("semantic_query"),
                })
    return cells


def _claim_sentences(answer: str) -> list[str]:
    return [value.strip() for value in re.split(r"(?<=[。！？!?;；])|\n+", answer) if value.strip()]


def _claim_numbers(text: str) -> list[dict[str, Any]]:
    output = []
    for match in NUMBER_RE.finditer(text):
        raw = match.group(0)
        plain = raw.replace(",", "").rstrip("%")
        try:
            value = float(plain)
        except ValueError:
            continue
        suffix = text[match.end(): match.end() + 1]
        # Calendar years are context, not quantitative claims.
        if value.is_integer() and 1900 <= value <= 2100 and (suffix == "年" or "-" in text[max(0, match.start() - 1):match.end() + 1]):
            continue
        output.append({"text": raw, "value": value, "percentage": raw.endswith("%")})
    return output


def _numbers_match(number: dict[str, Any], cell_value: float) -> bool:
    candidates = [number["value"]]
    if number["percentage"]:
        candidates.append(number["value"] / 100.0)
    return any(math.isclose(value, cell_value, rel_tol=1e-6, abs_tol=1e-9) for value in candidates)


def _build_claims(
    database: Database, workspace_id: str, answer: str, refs: list[str],
) -> list[dict[str, Any]]:
    cells = _evidence_cells(database, workspace_id, refs)
    claims = []
    for text in _claim_sentences(answer):
        numbers = _claim_numbers(text)
        matched = []
        unmatched = []
        for number in numbers:
            found = next((cell for cell in cells if _numbers_match(number, cell["value"])), None)
            if found:
                matched.append({"number": number["text"], **found})
            else:
                unmatched.append(number["text"])
        semantic_refs = list(dict.fromkeys(
            f"metric:{item['semantic_query']['metric_id']}@{item['semantic_query']['metric_version']}"
            for item in matched if item.get("semantic_query")
        ))
        claims.append({
            "text": text, "evidence_refs": refs, "evidence_cells": matched,
            "definition_refs": semantic_refs, "numbers": numbers,
            "unmatched_numbers": unmatched,
            "numeric_replay": "PASS" if not unmatched else "FAIL",
        })
    return claims


class ResultService:
    """Owns claims, manifests and the sole immutable publication gate."""

    def __init__(self, database: Database, authorize: Callable[[dict[str, Any]], bool] | None = None):
        self.db = database
        self.store = RunStore(database)
        self.authorize = authorize or (lambda _run: True)

    def finalize(self, run_id: str, answer: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        contract = self.store.latest_contract(run_id)
        if not run or not contract:
            return {"published": False, "quality_status": "blocked", "issues": ["run_or_contract_missing"]}
        authorized = self.authorize(run)
        rules = [
            Rule("contract_confirmed", "1", "execution", "blocking", 2, lambda _ctx: outcome(
                "PASS" if contract.get("confirmed_at") else "FAIL", "任务契约已确认" if contract.get("confirmed_at") else "任务契约未确认",
            )),
            Rule("current_authorization", "1", "execution", "blocking", 3, lambda _ctx: outcome(
                "PASS" if authorized else "FAIL", "当前授权有效" if authorized else "当前授权已撤销",
            )),
            Rule("tool_evidence", "1", "expression", "blocking", 3, lambda ctx: outcome(
                "PASS" if ctx["successful"] else "FAIL", "存在真实工具证据" if ctx["successful"] else "没有真实工具证据",
                successful=len(ctx["successful"]),
            )),
            Rule("tool_failures", "1", "execution", "blocking", 2, lambda ctx: outcome(
                "PASS" if not ctx["failed"] else "FAIL", "没有未解决的工具失败" if not ctx["failed"] else "存在未解决的工具失败",
                failed=ctx["failed"],
            )),
            Rule("result_completeness", "1", "data", "blocking", 3, lambda ctx: outcome(
                "PASS" if ctx["complete"] else "UNKNOWN", "结果范围完整" if ctx["complete"] else "结果完整性未知或部分",
            )),
            Rule("claim_provenance", "1", "expression", "blocking", 3, lambda ctx: outcome(
                "PASS" if ctx["refs"] else "FAIL", "结论绑定了证据引用" if ctx["refs"] else "结论没有证据引用",
            )),
            Rule("independent_validation", "1", "execution", "blocking", 3, lambda ctx: outcome(
                "PASS" if ctx["validated"] else "FAIL",
                "已执行独立结果验证" if ctx["validated"] else "未执行或未通过独立结果验证",
            )),
            Rule("numeric_claim_replay", "1", "expression", "blocking", 3, lambda ctx: outcome(
                "PASS" if all(item["numeric_replay"] == "PASS" for item in ctx["claims"]) else "FAIL",
                "回答中的数字均可从证据单元格复算"
                if all(item["numeric_replay"] == "PASS" for item in ctx["claims"])
                else "回答包含无法从证据单元格核对的数字",
                unmatched=[
                    {"claim": item["text"], "numbers": item["unmatched_numbers"]}
                    for item in ctx["claims"] if item["unmatched_numbers"]
                ],
            )),
        ]
        successful = [item for item in evidence if item.get("status") == "SUCCEEDED"]
        failed = [item.get("tool") for item in evidence if item.get("status") in {"FAILED", "UNKNOWN"}]
        all_refs = list(dict.fromkeys(ref for item in successful for ref in item.get("refs") or []))
        refs = list(dict.fromkeys(
            resolved
            for ref in all_refs
            for resolved in _expand_result_refs(self.db, run["workspace_id"], ref)
        ))
        referenced = [
            item for item in successful
            if any(ref in refs for ref in item.get("refs") or [])
        ]
        complete = bool(refs) and all(item.get("completeness") == "complete" for item in referenced)
        validated_refs = {
            resolved
            for item in successful if item.get("validation_status") == "PASS"
            for ref in item.get("refs") or []
            for resolved in _expand_result_refs(self.db, run["workspace_id"], ref)
        }
        validated = bool(refs) and set(refs).issubset(validated_refs)
        claims = _build_claims(self.db, run["workspace_id"], answer, refs)
        subject = refs[0] if refs else f"run:{run_id}"
        validation = ValidationEngine(self.db, rules).evaluate(
            run_id=run_id, workspace_id=run["workspace_id"], subject_ref=subject,
            context={
                "successful": successful, "failed": failed, "refs": refs,
                "complete": complete, "validated": validated, "claims": claims,
            },
        )
        manifest = self.create_manifest(run, contract, answer, refs, validation, claims)
        if validation["status"] != "PASS":
            return {
                "published": False, "quality_status": "failed" if validation["status"] == "FAIL" else "unknown",
                "manifest_id": manifest["id"], "validation": validation,
            }
        publication = self.publish(run, contract, manifest, validation)
        return {
            "published": True, "quality_status": "passed", "publication_id": publication["id"],
            "manifest_id": manifest["id"], "validation": validation,
        }

    def create_manifest(
        self,
        run: dict[str, Any],
        contract: dict[str, Any],
        answer: str,
        evidence_refs: list[str],
        validation: dict[str, Any],
        claims: list[dict[str, Any]],
    ) -> dict[str, Any]:
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 version FROM result_manifests WHERE run_id=?", (run["id"],),
            ).fetchone()
            version = int(row["version"])
            manifest_id = self.db.new_id("manifest")
            payload = build_manifest_payload(
                self.db, workspace_id=run["workspace_id"], contract=contract["payload"],
                answer=answer, evidence_refs=evidence_refs, validation=validation,
                dependency_fingerprint={
                    "contract": contract["payload"].get("fingerprint"),
                    "policy_version": run["policy_version"], "source_scope": run["source_scope"],
                    "provider_id": run.get("provider_id"), "skill_id": run.get("skill_id"),
                },
            )
            connection.execute(
                """INSERT INTO result_manifests(id,workspace_id,run_id,version,status,payload,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (manifest_id, run["workspace_id"], run["id"], version, "validated_draft", json.dumps(payload, ensure_ascii=False), utcnow()),
            )
            claim_ids = []
            claim_details = []
            for value in claims:
                claim_id = self.db.new_id("claim")
                claim = {
                    **value,
                    "validation_refs": [item["id"] for item in validation["items"]],
                    "status": (
                        "validated"
                        if validation["status"] == "PASS" and value["numeric_replay"] == "PASS"
                        else "draft"
                    ),
                }
                connection.execute(
                    """INSERT INTO claims(id,workspace_id,run_id,manifest_id,claim_type,payload,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        claim_id, run["workspace_id"], run["id"], manifest_id, "fact",
                        json.dumps(claim, ensure_ascii=False), utcnow(),
                    ),
                )
                claim_ids.append(claim_id)
                claim_details.append({"id": claim_id, **claim})
            payload["claims"] = claim_ids
            payload["claim_details"] = claim_details
            connection.execute(
                "UPDATE result_manifests SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False), manifest_id),
            )
        return {"id": manifest_id, "run_id": run["id"], "version": version, "status": "validated_draft", "payload": payload}

    def publish(self, run: dict, contract: dict, manifest: dict, validation: dict) -> dict[str, Any]:
        publication_id = self.db.new_id("publication")
        now = utcnow()
        payload = {
            "quality_score": validation["quality_score"], "coverage": validation["coverage"],
            "source_scope": run["source_scope"], "outcome": "complete",
        }
        try:
            with self.db.transaction() as connection:
                connection.execute(
                    """INSERT INTO publications(
                           id,workspace_id,run_id,manifest_id,contract_version,policy_version,payload,created_at
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        publication_id, run["workspace_id"], run["id"], manifest["id"],
                        contract["version"], run["policy_version"], json.dumps(payload, ensure_ascii=False), now,
                    ),
                )
                connection.execute("UPDATE result_manifests SET status='published' WHERE id=?", (manifest["id"],))
        except Exception:
            existing = self.publication(run["id"], workspace_id=run["workspace_id"])
            if existing:
                return existing
            raise
        return {"id": publication_id, "run_id": run["id"], "manifest_id": manifest["id"], "payload": payload, "created_at": now}

    def publication(self, run_id: str, *, workspace_id: str) -> dict[str, Any] | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM publications WHERE run_id=? AND workspace_id=?", (run_id, workspace_id),
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        return value

    def manifest(self, manifest_id: str, *, workspace_id: str) -> dict[str, Any] | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM result_manifests WHERE id=? AND workspace_id=?", (manifest_id, workspace_id),
            ).fetchone()
        if not row:
            return None
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        return value

    def claims(self, run_id: str, *, workspace_id: str) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM claims WHERE run_id=? AND workspace_id=? ORDER BY created_at", (run_id, workspace_id),
            ).fetchall()
        return [dict(row) | {"payload": json.loads(row["payload"])} for row in rows]
