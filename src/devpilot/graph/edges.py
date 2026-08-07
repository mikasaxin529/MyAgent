"""ChatFlow 式图的条件边。

新 SSE 图（build_chat_graph）的流转逻辑：
    START → route_model → planner → call_model
        call_model --should_continue--> tools | reflector | save_response
        tools → call_model_after_tool --should_continue_after_tool--> tools | reflector | save_response
        reflector --reflector_routing--> call_model | save_response
        save_response → extract_memory → compress_memory → END

三条条件边（对齐 ChatFlow graph/edges.py，裁掉 DB/缓存分支）：
- should_continue: call_model 后，看末消息有无 tool_calls。
- should_continue_after_tool: after_tool 后，看是否还要调工具（含每步上限保护）。
- reflector_routing: reflector 后，按 decision 决定继续/重试/收尾。
"""
from __future__ import annotations

# 每步最多工具调用次数（防 LLM 死循环调工具）。对齐 ChatFlow _MAX_TOOL_CALLS_PER_STEP。
_MAX_TOOL_CALLS_PER_STEP = 50


def _last_tool_calls(messages: list) -> list | None:
    """取末条消息的 tool_calls，兼容 dict（OpenAI 格式）与 ChatMessage 对象。

    state.messages 里既有 ws_chat 装配的 dict，也有节点 append 的 ChatMessage，
    故此 helper 统一两种形态的 tool_calls 读取。
    """
    last = messages[-1] if messages else None
    if last is None:
        return None
    if isinstance(last, dict):
        return last.get("tool_calls")
    return getattr(last, "tool_calls", None)


def should_continue(state: dict) -> str:
    """call_model 后：末消息有 tool_calls → tools；无 + 有 plan → reflector；无 → save_response。

    - 末条 assistant 消息带 tool_calls（LLM 决定调工具）→ 进 ToolNode 执行。
    - 无 tool_calls 且 plan 非空 → 进 reflector 判 done/continue/retry。
    - 无 tool_calls 且 plan 为空（chat 路由直接答）→ 直接到 save_response。
    """
    messages = state.get("messages") or []
    if _last_tool_calls(messages):
        return "tools"
    if state.get("plan"):
        return "reflector"
    return "save_response"


def should_continue_after_tool(state: dict) -> str:
    """call_model_after_tool 后：看是否还要调工具。

    - 末消息还有 tool_calls 且本步工具调用数 < 上限 → 继续 tools。
    - 达上限 → 强制进 reflector（防死循环）。
    - 无 tool_calls + 有 plan → reflector。
    - 无 tool_calls + 无 plan → save_response。
    """
    messages = state.get("messages") or []
    has_tool_calls = bool(_last_tool_calls(messages))
    tool_count = len(state.get("tool_messages") or [])
    if has_tool_calls and tool_count < _MAX_TOOL_CALLS_PER_STEP:
        return "tools"
    if state.get("plan"):
        return "reflector"
    return "save_response"


def reflector_routing(state: dict) -> str:
    """reflector 后：decision in {continue, retry} → call_model；否则 → save_response。

    - continue: 推进到下一步，重置 step_iterations，回 call_model 执行新步。
    - retry: 重试当前步（保持 current_step_index），回 call_model。
    - done: 收尾，进 save_response。
    """
    decision = (state.get("reflector_decision") or "done").lower()
    if decision in ("continue", "retry"):
        return "call_model"
    return "save_response"
