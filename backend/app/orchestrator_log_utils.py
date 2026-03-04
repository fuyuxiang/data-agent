"""
结构化日志工具 - 遵循 JSON Lines 格式
"""
import json
import time
from datetime import datetime
from typing import Any, Dict, Optional, List


def _sanitize_value(value: Any, max_len: int = 500) -> Any:
    """脱敏/截断值"""
    if value is None:
        return None

    if isinstance(value, str):
        if len(value) > max_len:
            return value[:200] + "..." + value[-50:]
        return value

    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            if any(s in k.lower() for s in ["key", "token", "password", "secret", "api_key"]):
                result[k] = "***REDACTED***"
            else:
                result[k] = _sanitize_value(v, max_len)
        return result

    if isinstance(value, list):
        return [_sanitize_value(v, max_len) for v in value[:3]]

    return value


class LogHelper:
    """日志辅助类"""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.run_start_ts = time.time()
        self.node_start_ts: Dict[str, float] = {}
        self.logs: List[Dict] = []

    def info(self, message: str):
        """输出信息日志"""
        print(f"[LogHelper] {message}")

    def start_node(self, step: str, inputs: Dict) -> Dict:
        """输出节点开始日志"""
        node_start_ts = time.time()
        self.node_start_ts[step] = node_start_ts
        elapsed_ms = int((node_start_ts - self.run_start_ts) * 1000)

        # 步骤名称映射
        step_names = {
            "intent_node": "识别意图",
            "semantic_node": "语义理解",
            "dispatcher_node": "意图调度",
            "sql_gen_node": "生成SQL",
            "sql_validate_node": "SQL校验",
            "sql_execute_node": "执行SQL",
            "format_node": "格式化结果",
        }
        step_name = step_names.get(step, step.replace("_node", ""))

        # 记录当前是第几步
        if not hasattr(self, 'step_count'):
            self.step_count = 0
        self.step_count += 1

        log = {
            "type": "node_start",
            "step": step,
            "step_num": self.step_count,
            "summary": f"[第{self.step_count}步] {step_name}...",
        }
        print(json.dumps(log, ensure_ascii=False))
        self.logs.append(log)
        return log

    def end_node(self, step: str, outputs: Dict, summary: str = None) -> Dict:
        """输出节点结束日志"""
        node_start_ts = self.node_start_ts.get(step, time.time())
        node_elapsed = int((time.time() - node_start_ts) * 1000)
        elapsed_ms = int((time.time() - self.run_start_ts) * 1000)

        # 步骤名称映射
        step_names = {
            "intent_node": "识别意图",
            "semantic_node": "语义理解",
            "dispatcher_node": "意图调度",
            "sql_gen_node": "生成SQL",
            "sql_validate_node": "SQL校验",
            "sql_execute_node": "执行SQL",
            "format_node": "格式化结果",
        }
        step_name = step_names.get(step, step.replace("_node", ""))
        step_num = getattr(self, 'step_count', 1)

        if step == "intent_node":
            intent = outputs.get("intent", "")
            summary = f"[第{step_num}步] {step_name} - 意图: {intent}" if intent else f"[第{step_num}步] {step_name} 完成"
        elif step == "sql_gen_node":
            sql = outputs.get("sql", "")
            if sql:
                summary = f"[第{step_num}步] {step_name}\n{sql}"
            else:
                summary = f"[第{step_num}步] {step_name} 完成"
        elif step == "sql_execute_node":
            row_count = outputs.get('row_count', 0)
            summary = f"[第{step_num}步] {step_name} - 返回 {row_count} 行"
        elif step == "sql_validate_node":
            valid = outputs.get("valid", False)
            summary = f"[第{step_num}步] {step_name} - {'通过' if valid else '未通过'}"
        elif step == "format_node":
            answer = outputs.get("answer_text", "")
            summary = f"[第{step_num}步] {step_name}\n{answer}" if answer else f"[第{step_num}步] {step_name} 完成"
        else:
            summary = summary or f"[第{step_num}步] {step_name} 完成"

        log = {
            "type": "node_end",
            "step": step,
            "step_num": step_num,
            "summary": summary,
        }
        print(json.dumps(log, ensure_ascii=False))
        self.logs.append(log)
        return log

    def error_node(self, step: str, error: Dict, summary: str = None) -> Dict:
        """输出节点错误日志"""
        node_start_ts = self.node_start_ts.get(step, time.time())
        node_elapsed = int((time.time() - node_start_ts) * 1000)
        elapsed_ms = int((time.time() - self.run_start_ts) * 1000)

        log = {
            "type": "node_error",
            "run_id": self.run_id,
            "step": step,
            "ts": datetime.utcnow().isoformat() + "Z",
            "elapsed_ms": elapsed_ms,
            "node_elapsed_ms": node_elapsed,
            "status": "fail",
            "summary": summary or error.get("message", "节点执行失败")[:120],
            "inputs": {},
            "outputs": {},
            "error": error
        }
        print(json.dumps(log, ensure_ascii=False))
        self.logs.append(log)
        return log

    def final_log(self, outputs: Optional[Dict]) -> Dict:
        """输出最终日志"""
        elapsed_ms = int((time.time() - self.run_start_ts) * 1000)
        if outputs is None:
            outputs = {}

        summary_parts = []
        if outputs.get("answer_text"):
            summary_parts.append(outputs["answer_text"][:80])
        if outputs.get("row_count"):
            summary_parts.append(f"{outputs['row_count']} 行")
        if outputs.get("chart_suggestion"):
            summary_parts.append(f"图表: {outputs['chart_suggestion']}")

        log = {
            "type": "final",
            "run_id": self.run_id,
            "step": "END",
            "ts": datetime.utcnow().isoformat() + "Z",
            "elapsed_ms": elapsed_ms,
            "node_elapsed_ms": elapsed_ms,
            "status": outputs.get("status", "success"),
            "summary": " | ".join(summary_parts) if summary_parts else "执行完成",
            "inputs": {},
            "outputs": _sanitize_value(outputs)
        }
        print(json.dumps(log, ensure_ascii=False))
        self.logs.append(log)
        return log

    def get_logs(self) -> List[Dict]:
        """获取所有日志"""
        return self.logs
