"""CallModelAfterTool 节点：综合工具结果（对齐 ChatFlow call_model_after_tool_node）。

读 coder 模型，基于 tool messages 继续推理。可继续产 tool_calls
（should_continue_after_tool 会判是否再进 tools）。无 tool_calls 则收尾当前步
result。_check_last_tool_failed：末条 tool message 含失败标记时不综合，
返 reflector_decision=retry 让 reflector 处理。
"""
from __future__ import annotations

from ...config import load_agent_models
from ...gateway import ChatMessage
from ..state import AgentGraphState
from .base import build_tools, done, emit, emit_thinking, ensure_date_system, visit
from .call_model_node import _finalize_acc, _merge_tool_delta


def make_call_model_after_tool_node(gateway, registry=None, emitter=None):
    async def after_tool_node(state: AgentGraphState) -> dict:
        idx = state.get("current_step_index", 0)
        visited = visit(state, "call_model_after_tool", emitter)
        route = state.get("route", "chat")
        plan = state.get("plan") or []
        models = load_agent_models()
        # 读 route 产出的 answer_model（与 tool_model 同源，多数场景一致）。
        answer_model_str = state.get("answer_model", "") or state.get("tool_model", "")
        if ":" in answer_model_str:
            provider, model = answer_model_str.split(":", 1)
        else:
            provider, model = models.get("coder", ("deepseek", "deepseek-chat"))

        msgs = list(state.get("messages") or [])
        # 注入带今日日期的 system prompt：综合工具结果时不得把近期日期当未来数据剔除。
        msgs = ensure_date_system(msgs)
        # 工具失败检测：末条 tool message 含失败标记 → 不综合，让 reflector retry。
        if _check_last_tool_failed(msgs):
            done(emitter, "call_model_after_tool")
            return {"reflector_decision": "retry",
                    "reflection": "工具调用失败，需重试当前步。",
                    "nodes_visited": visited}

        tools = build_tools(registry, route)
        content = ""
        acc: dict[int, dict] = {}
        async for chunk in gateway.stream_chat(
            msgs, provider=provider, model=model, temperature=0.4, tools=tools,
        ):
            if chunk.reasoning:
                emit_thinking(emitter, "call_model_after_tool", "reasoning",
                              chunk.reasoning, idx)
            if chunk.delta:
                content += chunk.delta
                emit(emitter, {"type": "content", "delta": chunk.delta, "step_id": "call_model_after_tool"})
            if chunk.tool_call_delta:
                _merge_tool_delta(acc, chunk.tool_call_delta, emitter, idx)

        if acc:
            tool_calls = _finalize_acc(acc, emitter)
            asst = ChatMessage("assistant", content, tool_calls=tool_calls)
            msgs.append(asst)
            done(emitter, "call_model_after_tool")
            return {"messages": msgs, "full_response": content,
                    "step_iterations": state.get("step_iterations", 0) + 1,
                    "nodes_visited": visited}

        asst = ChatMessage("assistant", content)
        msgs.append(asst)
        if plan and idx < len(plan):
            plan = [dict(s) for s in plan]
            plan[idx] = {**plan[idx], "status": "done", "result": content}
        done(emitter, "call_model_after_tool")
        return {"messages": msgs, "full_response": content, "plan": plan,
                "nodes_visited": visited}

    return after_tool_node


def _check_last_tool_failed(msgs: list) -> bool:
    """末条 tool message content 含失败标记 → True（让 reflector retry）。

    失败标记：content 以 "[" 开头（如 [websearch] ...）且含 失败/未配置/未注册。
    """
    for m in reversed(msgs):
        if isinstance(m, ChatMessage) and m.role == "tool":
            c = m.content or ""
            if c.startswith("[") and ("失败" in c or "未配置" in c or "未注册" in c):
                return True
            return False
    return False
