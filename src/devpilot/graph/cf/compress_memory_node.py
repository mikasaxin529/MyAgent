"""CompressMemory 节点：压缩当前会话历史存 JSONL（对齐 ChatFlow compress_memory，裁 DB）。

把当前会话 messages 持久化到 .devpilot/memory/last_conv.jsonl，供跨轮长期
上下文。无 DB——单机单进程。此处暂存原始 messages（不调 LLM 压缩，省一次
调用；后续可接 runtime/memory.py 的三段式压缩做摘要）。
"""
from __future__ import annotations

import json
from pathlib import Path

from ...gateway import ChatMessage
from ..state import AgentGraphState
from .base import done, emit, visit

_MEM_DIR = Path(".devpilot/memory")
_CONV_FILE = _MEM_DIR / "last_conv.jsonl"


def make_compress_memory_node(gateway, audit=None, emitter=None):
    async def compress_node(state: AgentGraphState) -> dict:
        visited = visit(state, "compress_memory", emitter)
        msgs = state.get("messages") or []
        _MEM_DIR.mkdir(parents=True, exist_ok=True)
        # 序列化消息（dict 直存，ChatMessage 调 to_dict）。
        serial = [m if isinstance(m, dict) else m.to_dict() for m in msgs]
        _CONV_FILE.write_text(
            json.dumps(serial, ensure_ascii=False), encoding="utf-8")
        emit(emitter, {"type": "memory", "kind": "compress", "count": len(serial)})
        done(emitter, "compress_memory")
        return {"nodes_visited": visited}

    return compress_node
