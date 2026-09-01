"""SaveResponse 节点：持久化 + 澄清检测（对齐 ChatFlow save_response_node，裁 DB）。

把 full_response 写入 final_answer（供 SSE 端点兜底），记审计（复用
governance/audit.py 的进程内 AuditLog）。无 DB，仅内存 + JSONL 文件。
"""
from __future__ import annotations

from ..state import AgentGraphState
from .base import done, emit, visit


def make_save_response_node(gateway, audit=None, emitter=None):
    async def save_response_node(state: AgentGraphState) -> dict:
        visited = visit(state, "save_response", emitter)
        full = state.get("full_response", "")
        emit(emitter, {"type": "status", "status": "saving"})
        if audit is not None:
            try:
                audit.record("llm_call", actor="save_response",
                             detail={"answer_len": len(full)})
            except Exception:  # noqa: BLE001
                pass
        done(emitter, "save_response")
        return {"final_answer": full, "nodes_visited": visited}

    return save_response_node
