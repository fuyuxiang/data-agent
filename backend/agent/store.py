from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from ..core.database import Database, utcnow
from .contracts import ExecutionStatus, RunContext, TaskContract


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _load(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


class RunStore:
    """Transactional storage for governed analysis runs.

    Long model and engine calls are deliberately outside these methods.  A
    decision/action intent and its budget reservation are durable before a
    caller executes an effect.
    """

    def __init__(self, database: Database):
        self.db = database

    def create_run(
        self,
        *,
        workspace_id: str,
        session_id: str,
        actor_id: str,
        source_scope: list[str] | tuple[str, ...],
        allowed_tool_ids: list[str] | tuple[str, ...],
        provider_id: str | None = None,
        skill_id: str | None = None,
        parent_run_id: str | None = None,
        run_kind: str = "analysis",
        budget: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        now = utcnow()
        normalized_key = str(idempotency_key or "").strip()[:200] or None
        with self.db.transaction() as connection:
            if normalized_key:
                existing = connection.execute(
                    "SELECT * FROM agent_runs WHERE workspace_id=? AND actor_id=? AND idempotency_key=?",
                    (workspace_id, actor_id, normalized_key),
                ).fetchone()
                if existing:
                    return self._run(dict(existing)), False
            if parent_run_id:
                parent = connection.execute(
                    "SELECT workspace_id FROM agent_runs WHERE id=?", (parent_run_id,),
                ).fetchone()
                if not parent or str(parent["workspace_id"]) != workspace_id:
                    raise ValueError("父任务不存在或不属于当前工作空间")
            run_id = self.db.new_id("run")
            connection.execute(
                """INSERT INTO agent_runs(
                       id,workspace_id,session_id,actor_id,idempotency_key,parent_run_id,run_kind,
                       execution_status,outcome,quality_status,contract_version,plan_version,
                       policy_version,source_scope,allowed_tool_ids,provider_id,skill_id,budget,usage,
                       version,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, workspace_id, session_id, actor_id, normalized_key, parent_run_id, run_kind,
                    ExecutionStatus.WAITING_INPUT.value, "unknown", "not_evaluated", 0, 0,
                    "agent-policy-v1", _json(list(dict.fromkeys(source_scope))),
                    _json(list(dict.fromkeys(allowed_tool_ids))), provider_id, skill_id,
                    _json(budget or self.default_budget()), _json(self.empty_usage()), 1, now, now,
                ),
            )
            row = connection.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        run = self._run(dict(row))
        self.append_event(run_id, "analysis.created", {"status": run["execution_status"]})
        return run, True

    @staticmethod
    def default_budget() -> dict[str, Any]:
        return {
            "model_tokens": 100_000,
            "tool_calls": 100,
            "wall_seconds": 1800,
            "result_bytes": 50 * 1024 * 1024,
            "concurrency": 4,
            "warehouse_scan_bytes": None,
            "remote_compute_seconds": None,
        }

    @staticmethod
    def empty_usage() -> dict[str, Any]:
        return {
            "model_tokens": 0,
            "tool_calls": 0,
            "wall_seconds": 0,
            "result_bytes": 0,
            "warehouse_scan_bytes": 0,
            "remote_compute_seconds": 0,
            "cost": None,
        }

    def get_run(self, run_id: str, *, workspace_id: str | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM agent_runs WHERE id=?"
        args: list[Any] = [run_id]
        if workspace_id is not None:
            query += " AND workspace_id=?"
            args.append(workspace_id)
        with self.db.connect() as connection:
            row = connection.execute(query, args).fetchone()
        return self._run(dict(row)) if row else None

    def list_runs(self, workspace_id: str, *, session_id: str | None = None, limit: int = 100) -> list[dict]:
        query = "SELECT * FROM agent_runs WHERE workspace_id=?"
        args: list[Any] = [workspace_id]
        if session_id:
            query += " AND session_id=?"
            args.append(session_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self.db.connect() as connection:
            rows = connection.execute(query, args).fetchall()
        return [self._run(dict(row)) for row in rows]

    def context(self, run_id: str, *, workspace_id: str | None = None) -> RunContext:
        run = self.get_run(run_id, workspace_id=workspace_id)
        if not run:
            raise FileNotFoundError("分析任务不存在")
        return RunContext(
            run_id=run["id"], session_id=run["session_id"], actor_id=run["actor_id"],
            workspace_id=run["workspace_id"], contract_version=run["contract_version"],
            policy_version=run["policy_version"], source_scope=tuple(run["source_scope"]),
            allowed_tool_ids=tuple(run["allowed_tool_ids"]), parent_run_id=run.get("parent_run_id"),
            budget_id=run["id"], lease_epoch=run["lease_epoch"],
        )

    def add_contract(
        self,
        run_id: str,
        contract: TaskContract,
        *,
        expected_version: int,
        confirmed_by: str | None = None,
    ) -> dict[str, Any]:
        now = utcnow()
        with self.db.transaction() as connection:
            run = connection.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise FileNotFoundError("分析任务不存在")
            current = int(run["contract_version"])
            if current != int(expected_version):
                raise ValueError(f"任务契约版本冲突：当前为 {current}")
            version = current + 1
            revision_id = self.db.new_id("contract")
            confirmed_at = now if confirmed_by else None
            payload = contract.to_dict() | {"fingerprint": contract.fingerprint()}
            connection.execute(
                """INSERT INTO task_contract_revisions(
                       id,run_id,version,payload,confirmed_by,confirmed_at,created_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (revision_id, run_id, version, _json(payload), confirmed_by, confirmed_at, now),
            )
            next_status = ExecutionStatus.QUEUED.value if confirmed_by else ExecutionStatus.WAITING_INPUT.value
            connection.execute(
                """UPDATE agent_runs SET contract_version=?,source_scope=?,budget=?,
                       execution_status=?,version=version+1,updated_at=? WHERE id=?""",
                (version, _json(list(contract.source_scope)), _json(contract.budget or _load(run["budget"], {})), next_status, now, run_id),
            )
        event = "contract.confirmed" if confirmed_by else "contract.revised"
        self.append_event(run_id, event, {"contract_version": version, "contract": payload})
        return {
            "id": revision_id, "run_id": run_id, "version": version, "payload": payload,
            "confirmed_by": confirmed_by, "confirmed_at": confirmed_at, "created_at": now,
        }

    def latest_contract(self, run_id: str) -> dict[str, Any] | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_contract_revisions WHERE run_id=? ORDER BY version DESC LIMIT 1", (run_id,),
            ).fetchone()
        return self._revision(dict(row)) if row else None

    def add_plan(self, run_id: str, payload: dict[str, Any], *, reason: str, expected_version: int) -> dict:
        self._validate_plan(payload)
        now = utcnow()
        with self.db.transaction() as connection:
            run = connection.execute("SELECT plan_version FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise FileNotFoundError("分析任务不存在")
            current = int(run["plan_version"])
            if current != int(expected_version):
                raise ValueError(f"分析计划版本冲突：当前为 {current}")
            version = current + 1
            revision_id = self.db.new_id("plan")
            connection.execute(
                "INSERT INTO plan_revisions(id,run_id,version,payload,reason,created_at) VALUES(?,?,?,?,?,?)",
                (revision_id, run_id, version, _json(payload), str(reason)[:2000], now),
            )
            connection.execute(
                "UPDATE agent_runs SET plan_version=?,version=version+1,updated_at=? WHERE id=?",
                (version, now, run_id),
            )
        self.append_event(run_id, "plan.revised", {"plan_version": version, "reason": reason, "plan": payload})
        return {"id": revision_id, "run_id": run_id, "version": version, "payload": payload, "reason": reason, "created_at": now}

    def latest_plan(self, run_id: str) -> dict | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM plan_revisions WHERE run_id=? ORDER BY version DESC LIMIT 1", (run_id,),
            ).fetchone()
        return self._revision(dict(row)) if row else None

    def record_decision(self, run_id: str, response: Any) -> dict:
        now = utcnow()
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 AS sequence FROM agent_decisions WHERE run_id=?", (run_id,),
            ).fetchone()
            sequence = int(row["sequence"])
            decision_id = self.db.new_id("decision")
            calls = [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in response.tool_calls
            ]
            connection.execute(
                """INSERT INTO agent_decisions(
                       id,run_id,sequence,model_protocol,model_name,finish_reason,content,tool_calls,usage,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id, run_id, sequence, response.protocol, response.model,
                    response.finish_reason, response.content, _json(calls), _json(response.usage), now,
                ),
            )
        self.append_event(run_id, "model.decision", {
            "decision_id": decision_id, "finish_reason": response.finish_reason,
            "tool_call_count": len(calls), "usage": response.usage,
        })
        return {"id": decision_id, "sequence": sequence, "tool_calls": calls}

    def begin_action(
        self,
        run_id: str,
        decision_id: str,
        logical_action_id: str,
        tool_id: str,
        arguments: dict[str, Any],
        *,
        lease_epoch: int,
        reserve_amount: float = 1,
        reserve_unit: str = "calls",
        reserve_kind: str = "tool_calls",
    ) -> tuple[dict, dict]:
        if reserve_kind not in self.default_budget():
            raise ValueError("未知预算类型")
        now = utcnow()
        arguments_json = _json(arguments)
        arguments_hash = hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()
        with self.db.transaction() as connection:
            run = connection.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise FileNotFoundError("分析任务不存在")
            if int(run["lease_epoch"]) != int(lease_epoch):
                raise PermissionError("运行租约已失效")
            allowed = set(_load(run["allowed_tool_ids"], []))
            if tool_id not in allowed:
                raise PermissionError(f"任务范围未授权工具：{tool_id}")
            existing = connection.execute(
                "SELECT * FROM agent_actions WHERE run_id=? AND logical_action_id=?",
                (run_id, logical_action_id),
            ).fetchone()
            if existing:
                if existing["arguments_hash"] != arguments_hash or existing["tool_id"] != tool_id:
                    raise ValueError("同一 logical_action_id 不得改变工具或参数")
                attempt_number = int(connection.execute(
                    "SELECT COALESCE(MAX(attempt_number),0)+1 n FROM action_attempts WHERE action_id=?",
                    (existing["id"],),
                ).fetchone()["n"])
                action_id = str(existing["id"])
            else:
                attempt_number = 1
                action_id = self.db.new_id("action")
                connection.execute(
                    """INSERT INTO agent_actions(
                           id,run_id,decision_id,logical_action_id,tool_id,arguments,arguments_hash,status,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (action_id, run_id, decision_id, logical_action_id, tool_id, arguments_json, arguments_hash, "pending", now, now),
                )
            budget = _load(run["budget"], {})
            usage = _load(run["usage"], {})
            reservations = [("tool_calls", 1.0, "calls")]
            if reserve_kind != "tool_calls":
                reservations.append((reserve_kind, reserve_amount, reserve_unit))
            reservation_id = ""
            for kind, amount, unit in reservations:
                limit = budget.get(kind)
                spent = float(usage.get(kind) or 0)
                held = connection.execute(
                    """SELECT COALESCE(SUM(amount),0) held FROM budget_reservations
                       WHERE run_id=? AND kind=? AND status='reserved'""", (run_id, kind),
                ).fetchone()["held"]
                if limit is not None and spent + float(held or 0) + amount > float(limit):
                    raise RuntimeError(f"{kind} 预算不足")
                current_reservation = self.db.new_id("budget")
                reservation_id = reservation_id or current_reservation
                connection.execute(
                    """INSERT INTO budget_reservations(
                           id,run_id,action_id,kind,amount,unit,status,created_at
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (current_reservation, run_id, action_id, kind, amount, unit, "reserved", now),
                )
            attempt_id = self.db.new_id("attempt")
            connection.execute(
                """INSERT INTO action_attempts(
                       id,action_id,attempt_number,lease_epoch,status,started_at
                   ) VALUES(?,?,?,?,?,?)""",
                (attempt_id, action_id, attempt_number, lease_epoch, "running", now),
            )
            connection.execute(
                "UPDATE agent_actions SET status='running',updated_at=? WHERE id=?", (now, action_id),
            )
        self.append_event(run_id, "action.submitted", {
            "action_id": action_id, "logical_action_id": logical_action_id,
            "attempt_id": attempt_id, "tool_id": tool_id,
        })
        return (
            {"id": action_id, "logical_action_id": logical_action_id, "tool_id": tool_id, "arguments": arguments},
            {"id": attempt_id, "reservation_id": reservation_id, "attempt_number": attempt_number},
        )

    def finish_action(
        self,
        run_id: str,
        action_id: str,
        attempt_id: str,
        reservation_id: str,
        *,
        lease_epoch: int,
        status: str,
        result: dict[str, Any],
        error_code: str | None = None,
        actual_cost: float = 1,
        external_job_id: str | None = None,
    ) -> None:
        if status not in {"succeeded", "accepted", "waiting_approval", "failed", "unknown"}:
            raise ValueError("动作状态无效")
        now = utcnow()
        with self.db.transaction() as connection:
            run = connection.execute("SELECT lease_epoch,usage FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not run or int(run["lease_epoch"]) != int(lease_epoch):
                raise PermissionError("运行租约已失效，拒绝写入动作结果")
            attempt = connection.execute(
                "SELECT action_id,lease_epoch FROM action_attempts WHERE id=?", (attempt_id,),
            ).fetchone()
            if not attempt or attempt["action_id"] != action_id or int(attempt["lease_epoch"]) != int(lease_epoch):
                raise PermissionError("动作尝试不属于当前租约")
            connection.execute(
                """UPDATE action_attempts SET status=?,result=?,error_code=?,finished_at=? WHERE id=?""",
                (status, _json(result), error_code, now, attempt_id),
            )
            connection.execute(
                """UPDATE agent_actions SET status=?,result=?,error_code=?,external_job_id=?,updated_at=? WHERE id=?""",
                (status, _json(result), error_code, external_job_id, now, action_id),
            )
            reservations = connection.execute(
                "SELECT id,kind,amount FROM budget_reservations WHERE action_id=? AND status='reserved'",
                (action_id,),
            ).fetchall()
            for reservation in reservations:
                actual = 1 if reservation["kind"] == "tool_calls" else actual_cost
                connection.execute(
                    """UPDATE budget_reservations SET status='settled',actual=?,settled_at=? WHERE id=?""",
                    (actual, now, reservation["id"]),
                )
            usage = _load(run["usage"], self.empty_usage())
            usage["tool_calls"] = int(usage.get("tool_calls") or 0) + 1
            for reservation in reservations:
                if reservation["kind"] != "tool_calls":
                    usage[reservation["kind"]] = float(usage.get(reservation["kind"]) or 0) + float(actual_cost)
            connection.execute(
                "UPDATE agent_runs SET usage=?,updated_at=?,version=version+1 WHERE id=?",
                (_json(usage), now, run_id),
            )
        self.append_event(run_id, f"action.{status}", {
            "action_id": action_id, "attempt_id": attempt_id, "tool_id": result.get("tool"),
            "error_code": error_code, "external_job_id": external_job_id,
        })

    def complete_external_action(
        self, run_id: str, external_job_id: str, *, status: str,
        result: dict[str, Any], error_code: str | None = None,
    ) -> dict[str, Any]:
        """Reconcile a durable external job without re-spending the original action budget."""
        if status not in {"succeeded", "failed", "unknown", "cancelled"}:
            raise ValueError("外部动作状态无效")
        now = utcnow()
        with self.db.transaction() as connection:
            run = connection.execute("SELECT execution_status FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            action = connection.execute(
                "SELECT * FROM agent_actions WHERE run_id=? AND external_job_id=?",
                (run_id, external_job_id),
            ).fetchone()
            if not run or not action:
                raise FileNotFoundError("外部作业对应的 Agent Action 不存在")
            if action["status"] in {"succeeded", "failed", "cancelled"}:
                return self._action(dict(action))
            connection.execute(
                "UPDATE agent_actions SET status=?,result=?,error_code=?,updated_at=? WHERE id=?",
                (status, _json(result), error_code, now, action["id"]),
            )
            next_execution_status = (
                "cancelled" if status == "cancelled"
                else "paused" if run["execution_status"] == "paused"
                else "queued"
            )
            connection.execute(
                "UPDATE agent_runs SET execution_status=?,stop_reason=?,updated_at=?,version=version+1 WHERE id=?",
                (
                    next_execution_status,
                    "external_job_cancelled" if status == "cancelled"
                    else "external_job_reconciled_while_paused" if next_execution_status == "paused"
                    else "external_job_reconciled",
                    now, run_id,
                ),
            )
            updated = connection.execute("SELECT * FROM agent_actions WHERE id=?", (action["id"],)).fetchone()
        self.append_event(run_id, f"action.{status}", {
            "action_id": action["id"], "external_job_id": external_job_id,
            "error_code": error_code, "reconciled": True,
        })
        return self._action(dict(updated))

    def add_model_usage(self, run_id: str, usage_delta: dict[str, int]) -> None:
        delta = int(usage_delta.get("total_tokens") or 0)
        with self.db.transaction() as connection:
            row = connection.execute("SELECT usage,budget FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise FileNotFoundError("分析任务不存在")
            usage = _load(row["usage"], self.empty_usage())
            budget = _load(row["budget"], {})
            next_total = int(usage.get("model_tokens") or 0) + delta
            if budget.get("model_tokens") is not None and next_total > int(budget["model_tokens"]):
                raise RuntimeError("模型 Token 预算不足")
            usage["model_tokens"] = next_total
            connection.execute(
                "UPDATE agent_runs SET usage=?,updated_at=?,version=version+1 WHERE id=?",
                (_json(usage), utcnow(), run_id),
            )

    def update_status(
        self,
        run_id: str,
        status: str,
        *,
        outcome: str | None = None,
        quality_status: str | None = None,
        stop_reason: str | None = None,
        expected_version: int | None = None,
    ) -> dict:
        if status not in {item.value for item in ExecutionStatus}:
            raise ValueError("运行状态无效")
        now = utcnow()
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise FileNotFoundError("分析任务不存在")
            if expected_version is not None and int(row["version"]) != int(expected_version):
                raise ValueError(f"任务版本冲突：当前为 {row['version']}")
            values = {
                "execution_status": status,
                "outcome": outcome if outcome is not None else row["outcome"],
                "quality_status": quality_status if quality_status is not None else row["quality_status"],
                "stop_reason": stop_reason if stop_reason is not None else row["stop_reason"],
                "started_at": row["started_at"] or (now if status == "running" else None),
                "finished_at": now if status in {"finished", "failed", "cancelled"} else row["finished_at"],
            }
            connection.execute(
                """UPDATE agent_runs SET execution_status=?,outcome=?,quality_status=?,stop_reason=?,
                       started_at=?,finished_at=?,updated_at=?,version=version+1 WHERE id=?""",
                (*values.values(), now, run_id),
            )
            updated = connection.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
        self.append_event(run_id, "analysis.status", {
            "execution_status": status, "outcome": values["outcome"],
            "quality_status": values["quality_status"], "stop_reason": values["stop_reason"],
        })
        return self._run(dict(updated))

    def acquire_lease(self, run_id: str, owner: str, *, ttl_seconds: int = 60) -> RunContext:
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=max(10, ttl_seconds))).isoformat(timespec="seconds")
        with self.db.transaction() as connection:
            row = connection.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise FileNotFoundError("分析任务不存在")
            current_expiry = row["lease_expires_at"]
            if row["lease_owner"] and row["lease_owner"] != owner and current_expiry and current_expiry > now.isoformat(timespec="seconds"):
                raise RuntimeError("任务正由其他 Runner 执行")
            epoch = int(row["lease_epoch"]) + 1
            connection.execute(
                """UPDATE agent_runs SET lease_owner=?,lease_epoch=?,lease_expires_at=?,updated_at=? WHERE id=?""",
                (owner, epoch, expires, utcnow(), run_id),
            )
        return self.context(run_id)

    def heartbeat(self, run_id: str, owner: str, lease_epoch: int, *, ttl_seconds: int = 60) -> bool:
        expires = (datetime.now(timezone.utc) + timedelta(seconds=max(10, ttl_seconds))).isoformat(timespec="seconds")
        with self.db.transaction() as connection:
            cursor = connection.execute(
                """UPDATE agent_runs SET lease_expires_at=?,updated_at=?
                   WHERE id=? AND lease_owner=? AND lease_epoch=?""",
                (expires, utcnow(), run_id, owner, lease_epoch),
            )
        return cursor.rowcount == 1

    def release_lease(self, run_id: str, owner: str, lease_epoch: int) -> bool:
        """Release only the lease held by this exact runner epoch.

        Epoch fencing remains monotonic: a later runner can acquire a new epoch,
        while a stale runner can no longer heartbeat or persist action results.
        """
        with self.db.transaction() as connection:
            cursor = connection.execute(
                """UPDATE agent_runs SET lease_owner=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE id=? AND lease_owner=? AND lease_epoch=?""",
                (utcnow(), run_id, owner, int(lease_epoch)),
            )
        if cursor.rowcount:
            self.append_event(run_id, "lease.released", {"owner": owner, "lease_epoch": int(lease_epoch)})
        return cursor.rowcount == 1

    def append_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> dict:
        run = self.get_run(run_id)
        if not run:
            raise FileNotFoundError("分析任务不存在")
        event_id = self.db.new_id("event")
        now = utcnow()
        with self.db.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO run_events(event_id,run_id,workspace_id,schema_version,event_type,payload,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (event_id, run_id, run["workspace_id"], 1, event_type, _json(payload), now),
            )
            sequence = int(cursor.lastrowid)
        return {
            "event_id": event_id, "run_id": run_id, "workspace_id": run["workspace_id"],
            "sequence": sequence, "schema_version": 1, "type": event_type,
            "payload": payload, "created_at": now,
        }

    def events(self, run_id: str, *, after: int = 0, limit: int = 500) -> list[dict]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM run_events WHERE run_id=? AND sequence>?
                   ORDER BY sequence ASC LIMIT ?""",
                (run_id, max(0, int(after)), max(1, min(int(limit), 2000))),
            ).fetchall()
        return [{
            "event_id": row["event_id"], "run_id": row["run_id"], "workspace_id": row["workspace_id"],
            "sequence": row["sequence"], "schema_version": row["schema_version"],
            "type": row["event_type"], "payload": _load(row["payload"], {}), "created_at": row["created_at"],
        } for row in rows]

    def actions(self, run_id: str) -> list[dict]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_actions WHERE run_id=? ORDER BY created_at", (run_id,),
            ).fetchall()
        return [self._action(dict(row)) for row in rows]

    def decisions(self, run_id: str) -> list[dict]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_decisions WHERE run_id=? ORDER BY sequence", (run_id,),
            ).fetchall()
        return [{
            **dict(row), "tool_calls": _load(row["tool_calls"], []), "usage": _load(row["usage"], {}),
        } for row in rows]

    @staticmethod
    def _validate_plan(payload: dict[str, Any]) -> None:
        tasks = payload.get("tasks") or []
        if not isinstance(tasks, list) or len(tasks) > 200:
            raise ValueError("计划 tasks 必须是最多 200 项的数组")
        ids = [str(item.get("id") or "") for item in tasks if isinstance(item, dict)]
        if any(not item for item in ids) or len(ids) != len(tasks) or len(ids) != len(set(ids)):
            raise ValueError("每个计划任务必须有唯一 id")
        graph = {
            str(item["id"]): [str(value) for value in item.get("depends_on") or []]
            for item in tasks
        }
        if any(dependency not in graph for values in graph.values() for dependency in values):
            raise ValueError("计划引用了不存在的依赖")
        visiting: set[str] = set()
        visited: set[str] = set()

        def walk(node: str) -> None:
            if node in visiting:
                raise ValueError("分析计划不能包含依赖环")
            if node in visited:
                return
            visiting.add(node)
            for dependency in graph[node]:
                walk(dependency)
            visiting.remove(node)
            visited.add(node)

        for node in graph:
            walk(node)

    @staticmethod
    def _run(row: dict[str, Any]) -> dict[str, Any]:
        for key, default in (
            ("source_scope", []), ("allowed_tool_ids", []), ("budget", {}), ("usage", {}),
        ):
            row[key] = _load(row.get(key), default)
        return row

    @staticmethod
    def _revision(row: dict[str, Any]) -> dict[str, Any]:
        row["payload"] = _load(row.get("payload"), {})
        return row

    @staticmethod
    def _action(row: dict[str, Any]) -> dict[str, Any]:
        row["arguments"] = _load(row.get("arguments"), {})
        row["result"] = _load(row.get("result"), None)
        return row
