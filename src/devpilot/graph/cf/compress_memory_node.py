"""CompressMemory 节点：会话滚动摘要存中期记忆（对齐 ChatFlow compress_memory）。

会话超过阈值轮时，把窗口外的早期消息 LLM 压缩成一段摘要，存
.devpilot/store.db 的 summaries 表（session_id + 覆盖到的消息上界 seq）。
供下次重建上下文时前置摘要 + 最近原文（对应 runtime/memory.py 三段式）。
短会话不触发——消息本体已由前端整段落进 messages 表。
"""
from __future__ import annotations

import json
from pathlib import Path

from ...gateway import ChatMessage
from ..state import AgentGraphState
from .base import done, emit, visit

_MEM_DIR = Path(".devpilot/memory")
_CONV_FILE = _MEM_DIR / "last_conv.jsonl"

# 触发摘要压缩的最小消息数（含 system；低阈值没必要压缩）。
_COMPRESS_THRESHOLD = 20
# 摘要时窗口外保留的最近消息条数。
_KEEP_RECENT = 6


def make_compress_memory_node(gateway, audit=None, emitter=None):
    async def compress_node(state: AgentGraphState) -> dict:
        visited = visit(state, "compress_memory", emitter)
        msgs = state.get("messages") or []
        serial = [m if isinstance(m, dict) else m.to_dict() for m in msgs]
        session_id = state.get("session_id", "")

        if session_id and len(serial) >= _COMPRESS_THRESHOLD:
            middle = serial[:-_KEEP_RECENT]
            summary = await _summarize(gateway, middle)
            if summary:
                try:
                    from ...web import store
                    store.save_summary(session_id, len(middle), summary)
                    emit(emitter, {"type": "memory", "kind": "compress",
                                   "count": len(serial), "summary_chars": len(summary)})
                except Exception:  # noqa: BLE001 - 存储失败不阻断主链路
                    pass
        done(emitter, "compress_memory")
        return {"nodes_visited": visited}

    return compress_node


async def _summarize(gateway, messages: list[dict]) -> str:
    """把一段历史压成一段忠实摘要。gateway 异常返回空串（跳过压缩）。"""
    system = (
        "你是记忆压缩器。把以下 Agent 历史轨迹压缩成一段保留关键事实与决策的摘要，"
        "不要编造，保留：用户目标、已做决策、关键观察、未完成事项。直接输出摘要文本。"
    )
    transcript = "\n".join(
        f"[{m.get('role', '?')}] {str(m.get('content', ''))[:800]}" for m in messages
    )
    raw = ""
    try:
        async for chunk in gateway.stream_chat(
            [ChatMessage("system", system), ChatMessage("user", transcript)],
            temperature=0.0,
        ):
            if chunk.delta:
                raw += chunk.delta
    except Exception:  # noqa: BLE001
        return ""
    return raw.strip()
