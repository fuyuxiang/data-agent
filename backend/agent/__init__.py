"""Governed, Flask-independent agent runtime.

The package owns the only model/tool decision loop.  HTTP, scheduled workflows,
and child analyses construct trusted contexts and delegate here.
"""

from .contracts import RunContext, TaskContract, ToolResult, ToolStatus
from .loop import AgentLoop
from .store import RunStore

__all__ = ["AgentLoop", "RunContext", "RunStore", "TaskContract", "ToolResult", "ToolStatus"]
