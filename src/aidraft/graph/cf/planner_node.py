"""Planner 节点：动态生成步骤计划（对齐 ChatFlow planner_node，裁掉 DB/续写/force_plan）。

零 few-shot + 不变量 + schema 驱动（学 ChatFlow planner.md）。
只产步骤 title/description，不指定 tool——步骤内 LLM 自主 function-calling 调工具。
chat 路由直接返空 plan（call_model 直接答，不绑工具）。
"""
from __future__ import annotations

import json
import re
from datetime import date as _date

from ..nodes import _recent_history_text
from ...config import load_agent_models
from ...gateway import ChatMessage
from ...prompts import load_prompt
from ..state import AgentGraphState
from .base import done, emit, emit_thinking, step_id, visit

_MAX_PLAN_STEPS = 10


def make_planner_node(gateway, registry=None, emitter=None):
    async def planner_node(state: AgentGraphState) -> dict:
        visited = visit(state, "planner", emitter)
        task = state.get("user_message") or state.get("task", "")
        route = state.get("route", "chat")

        # chat 路由直接答：返空 plan，call_model 不绑工具直接流式答。
        if route == "chat":
            emit(emitter, {"type": "plan", "steps": []})
            done(emitter, "planner")
            return {"plan": [], "current_step_index": 0, "step_iterations": 0,
                    "nodes_visited": visited}

        models = load_agent_models()
        provider, model = models.get("planner", ("deepseek", "deepseek-chat"))

        today = _date.today().strftime("%Y年%m月%d日")
        system = load_prompt("planner", today=today)
        prompt = "<USER_REQUEST>\n" + task + "\n</USER_REQUEST>"
        hist = _recent_history_text(state, 4)
        if hist:
            prompt += f"\n\n[对话历史]\n{hist}"

        raw = ""
        async for chunk in gateway.stream_chat(
            [ChatMessage("system", system), ChatMessage("user", prompt)],
            provider=provider, model=model, temperature=0.2,
        ):
            if chunk.reasoning:
                emit_thinking(emitter, "planner", "reasoning", chunk.reasoning)
            if chunk.delta:
                raw += chunk.delta
                emit_thinking(emitter, "planner", "content", chunk.delta)

        plan = _parse_plan(raw)
        emit(emitter, {"type": "plan", "steps": plan})
        done(emitter, "planner")
        return {"plan": plan, "current_step_index": 0, "step_iterations": 0,
                "nodes_visited": visited}

    return planner_node


def _parse_plan(raw: str) -> list[dict]:
    """从 LLM 输出解析步骤计划，容错返空（call_model 兜底直答）。

    plan 结构 [{id,title,description,status,result}]，status 初始 "pending"。
    解析失败返 [] —— call_model 看到 plan 空则直接流式回答（不绑工具）。
    """
    text = (raw or "").strip().strip("`").strip()
    text = re.sub(r"^json\s*", "", text, flags=re.IGNORECASE)
    try:
        data = json.loads(text)
        steps = data.get("steps") if isinstance(data, dict) else None
        if isinstance(steps, list):
            norm = []
            for i, s in enumerate(steps):
                if not isinstance(s, dict):
                    continue
                if i >= _MAX_PLAN_STEPS:
                    break
                norm.append({
                    "id": step_id(i),
                    "title": str(s.get("title", f"步骤{i+1}"))[:20],
                    "description": str(s.get("description", ""))[:400],
                    "status": "pending",
                    "result": "",
                })
            return norm
    except Exception:  # noqa: BLE001
        pass
    return []
