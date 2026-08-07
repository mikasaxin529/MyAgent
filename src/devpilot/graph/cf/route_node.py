"""Route 节点：5 标签路由（对齐 ChatFlow route_node，裁掉 DB/缓存/vision）。

5 标签：search_code/search/finance/code/chat。temp=0.0 稳定。
产出 route + tool_model + answer_model（前端展示来源 + call_model 据 route 绑工具）。
"""
from __future__ import annotations

import re
from datetime import date as _date

from ..nodes import _recent_history_text  # noqa: F401  (复用旧 nodes 历史辅助，planner 用)
from ...config import load_agent_models
from ...gateway import ChatMessage
from ...prompts import load_prompt
from ..state import AgentGraphState
from .base import done, emit, emit_thinking, visit

# search_code 必须在 search 前，防部分匹配（"search" 是 "search_code" 子串）。
_ROUTE_CANDIDATES = ("search_code", "search", "finance", "code", "chat")

# 5 标签 → (工具模型 key, 回答模型 key, 是否给 call_model 绑工具)
# search 类绑 websearch/weather/fetch 工具；code/chat 不绑（直接答/写码）。
ROUTE_MODEL_MAP: dict[str, tuple[str, str, bool]] = {
    "search_code": ("coder", "coder", True),
    "search": ("coder", "coder", True),
    "finance": ("coder", "coder", True),
    "code": ("coder", "coder", False),
    "chat": ("chat", "chat", False),
}


def make_route_node(gateway, registry=None, emitter=None):
    async def route_node(state: AgentGraphState) -> dict:
        visited = visit(state, "route_model", emitter)
        task = state.get("task") or state.get("user_message", "")
        models = load_agent_models()
        provider, model = models.get("router", ("ollama", "qwen2.5:7b"))

        today = _date.today().strftime("%Y年%m月%d日")
        system = load_prompt("route", today=today)

        raw = ""
        async for chunk in gateway.stream_chat(
            [ChatMessage("system", system), ChatMessage("user", task)],
            provider=provider, model=model, temperature=0.0,
        ):
            if chunk.reasoning:
                emit_thinking(emitter, "route_model", "reasoning", chunk.reasoning)
            if chunk.delta:
                raw += chunk.delta
                emit_thinking(emitter, "route_model", "content", chunk.delta)

        route = _parse_route(raw)
        tool_key, answer_key, _bind = ROUTE_MODEL_MAP.get(route, ROUTE_MODEL_MAP["chat"])
        tp, tm = models.get(tool_key, ("deepseek", "deepseek-chat"))
        ap, am = models.get(answer_key, ("deepseek", "deepseek-chat"))
        tool_model = f"{tp}:{tm}"
        answer_model = f"{ap}:{am}"
        emit(emitter, {"type": "route", "route": route,
                        "tool_model": tool_model, "answer_model": answer_model})
        done(emitter, "route_model")
        return {
            "route": route,
            "tool_model": tool_model,
            "answer_model": answer_model,
            "user_message": task,
            "nodes_visited": visited,
        }

    return route_node


def _parse_route(text: str) -> str:
    """从 LLM 输出解析 route 标签，容错兜底 chat。"""
    t = (text or "").strip().strip("`").strip().lower()
    # 去掉可能的 markdown 包裹。
    t = re.sub(r"^json\s*", "", t, flags=re.IGNORECASE).strip()
    for c in _ROUTE_CANDIDATES:
        if c in t:
            return c
    return "chat"
