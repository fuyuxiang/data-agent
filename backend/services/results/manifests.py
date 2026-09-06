from __future__ import annotations

import json
from typing import Any, Callable

from ...agent.store import RunStore
from ...core.database import Database, utcnow
from ..validation.engine import Rule, ValidationEngine, outcome
from .rendering import build_manifest_payload


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
        ]
        successful = [item for item in evidence if item.get("status") == "SUCCEEDED"]
        failed = [item.get("tool") for item in evidence if item.get("status") in {"FAILED", "UNKNOWN"}]
        refs = list(dict.fromkeys(ref for item in successful for ref in item.get("refs") or []))
        referenced = [item for item in successful if item.get("refs")]
        complete = bool(referenced) and all(item.get("completeness") == "complete" for item in referenced)
        validated = any(item.get("validation_status") == "PASS" for item in successful)
        subject = refs[0] if refs else f"run:{run_id}"
        validation = ValidationEngine(self.db, rules).evaluate(
            run_id=run_id, workspace_id=run["workspace_id"], subject_ref=subject,
            context={
                "successful": successful, "failed": failed, "refs": refs,
                "complete": complete, "validated": validated,
            },
        )
        manifest = self.create_manifest(run, contract, answer, refs, validation)
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
            claim_id = self.db.new_id("claim")
            claim = {
                "text": answer, "evidence_refs": evidence_refs, "definition_refs": [],
                "validation_refs": [item["id"] for item in validation["items"]], "status": "validated" if validation["status"] == "PASS" else "draft",
            }
            connection.execute(
                """INSERT INTO claims(id,workspace_id,run_id,manifest_id,claim_type,payload,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (claim_id, run["workspace_id"], run["id"], manifest_id, "fact", json.dumps(claim, ensure_ascii=False), utcnow()),
            )
            payload["claims"] = [claim_id]
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
