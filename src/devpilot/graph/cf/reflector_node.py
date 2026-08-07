"""Reflector 节点：判 done/continue/retry（对齐 ChatFlow reflector_node）。

快速路径优先（无 LLM 调用，省时省钱）：
- after_tool 已标 retry（工具失败）→ retry
- 无 plan（chat 路由直接答）→ done
- 末步 + 有 full_response → done
- 非末步 + 有 full_response + 首次（step_iterations<=1）→ continue
仅都不命中时才调 LLM 评估（step_iterations>0 的边界场景）。
"""
from __future__ import annotations

import json
import re

from ...config import load_agent_models
from ...gateway import ChatMessage
from ...prompts import load_prompt
from ..state import AgentGraphState
from .base import done, emit, emit_thinking, visit


def make_reflector_node(gateway, registry=None, emitter=None):
    async def reflector_node(state: AgentGraphState) -> dict:
        visited = visit(state, "reflector", emitter)
        plan = state.get("plan") or []
        idx = state.get("current_step_index", 0)
        full = state.get("full_response", "")
        step_iters = state.get("step_iterations", 0)

        # after_tool 已标 retry（工具失败）→ 直接 retry。
        if state.get("reflector_decision") == "retry":
            reason = state.get("reflection") or "工具调用失败，重试当前步。"
            emit(emitter, {"type": "reflection", "content": reason, "decision": "retry"})
            done(emitter, "reflector")
            return {"nodes_visited": visited}

        # 快速路径
        if not plan:
            decision, reason = "done", "无计划（chat 路由），回答完成。"
        elif idx >= len(plan) - 1 and full:
            decision, reason = "done", "最后一步已产出有效结果，完成。"
        elif full and step_iters <= 1:
            decision, reason = "continue", "当前步完成，推进下一步。"
        else:
            decision, reason = await _llm_reflect(gateway, state, emitter)

        emit(emitter, {"type": "reflection", "content": reason, "decision": decision})
        update: dict = {"reflector_decision": decision, "reflection": reason,
                        "nodes_visited": visited}
        if decision == "continue":
            update["current_step_index"] = idx + 1
            update["step_iterations"] = 0
        # retry 保持 current_step_index 不变（重试当前步）。
        done(emitter, "reflector")
        return update

    return reflector_node


async def _llm_reflect(gateway, state: dict, emitter) -> tuple[str, str]:
    """LLM 评估 decision（仅快速路径不命中时调用）。"""
    models = load_agent_models()
    provider, model = models.get("reflector", models.get("coder", ("deepseek", "deepseek-chat")))
    system = load_prompt("reflector")
    plan = state.get("plan") or []
    idx = state.get("current_step_index", 0)
    full = (state.get("full_response") or "")[:1000]
    step_desc = plan[idx].get("description", "") if idx < len(plan) else ""
    is_last = idx >= len(plan) - 1
    prompt = (
        f"当前步骤 {idx + 1}/{len(plan)}：{step_desc}\n"
        f"是否最后一步：{is_last}\n回复内容：{full}\n判断 decision。"
    )
    raw = ""
    async for chunk in gateway.stream_chat(
        [ChatMessage("system", system), ChatMessage("user", prompt)],
        provider=provider, model=model, temperature=0.0,
    ):
        if chunk.reasoning:
            emit_thinking(emitter, "reflector", "reasoning", chunk.reasoning)
        if chunk.delta:
            raw += chunk.delta
    t = raw.strip().strip("`").strip()
    t = re.sub(r"^json\s*", "", t, flags=re.IGNORECASE)
    try:
        data = json.loads(t)
        if isinstance(data, dict):
            d = str(data.get("decision", "done")).lower()
            r = str(data.get("reason", ""))
            if d in ("done", "continue", "retry"):
                return d, r
    except Exception:  # noqa: BLE001
        pass
    low = t.lower()
    for d in ("retry", "continue", "done"):
        if d in low:
            return d, t[:100]
    return "done", t[:100]
