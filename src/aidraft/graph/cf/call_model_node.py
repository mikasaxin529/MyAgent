"""CallModel 节点：主推理 + 原生 function-calling（对齐 ChatFlow call_model_node）。

读 coder 模型，据 route 绑工具，stream_chat 带 tools。流式 tool_calls 增量聚合
（acc dict[index] 合并 name + append arguments），逐片 emit thinking/content/
tool_call_args。流结束：有 tool_calls → 追加 assistant(tool_calls) 消息等 ToolNode
执行；无 tool_calls → full_response = content，标记当前步 done。
"""
from __future__ import annotations

import json

from ..nodes import _state_messages_full
from ...config import load_agent_models
from ...gateway import ChatMessage
from ..state import AgentGraphState
from .base import build_tools, done, emit, emit_thinking, ensure_date_system, visit


def make_call_model_node(gateway, registry=None, emitter=None):
    async def call_model_node(state: AgentGraphState) -> dict:
        idx = state.get("current_step_index", 0)
        visited = visit(state, "call_model", emitter)
        task = state.get("user_message") or state.get("task", "")
        route = state.get("route", "chat")
        plan = state.get("plan") or []

        models = load_agent_models()
        # 读 route 产出的 tool_model（"provider:model"），chat 路由→ollama，
        # search/code 路由→deepseek。缺失回退 coder。
        tool_model_str = state.get("tool_model", "")
        if ":" in tool_model_str:
            provider, model = tool_model_str.split(":", 1)
        else:
            provider, model = models.get("coder", ("deepseek", "deepseek-chat"))

        # 消息构造：步0 用全量历史（含原始 user prompt）；步1+ 追加步骤指令聚焦。
        msgs = list(state.get("messages") or [])
        if not msgs:
            msgs = _state_messages_full(state, task)
        # 注入带今日日期的 system prompt，避免模型把近期搜索结果当未来数据剔除，
        # 并让相对时间词以今天为基准（否则 deepseek 会搜“2025年7月”并剔 2026）。
        msgs = ensure_date_system(msgs)
        if plan and idx < len(plan) and idx > 0:
            step = plan[idx]
            msgs.append(ChatMessage(
                "user",
                f"[当前步骤] {step.get('title', '')}: {step.get('description', '')}",
            ))

        tools = build_tools(registry, route)

        content = ""
        acc: dict[int, dict] = {}
        async for chunk in gateway.stream_chat(
            msgs, provider=provider, model=model, temperature=0.4, tools=tools,
        ):
            if chunk.reasoning:
                emit_thinking(emitter, "call_model", "reasoning", chunk.reasoning, idx)
            if chunk.delta:
                content += chunk.delta
                emit(emitter, {"type": "content", "delta": chunk.delta, "step_id": "call_model"})
            if chunk.tool_call_delta:
                _merge_tool_delta(acc, chunk.tool_call_delta, emitter, idx)

        if acc:
            tool_calls = _finalize_acc(acc, emitter)
            asst = ChatMessage("assistant", content, tool_calls=tool_calls)
            msgs.append(asst)
            done(emitter, "call_model")
            return {"messages": msgs, "full_response": content,
                    "step_iterations": state.get("step_iterations", 0) + 1,
                    "nodes_visited": visited}

        # 无 tool_calls：纯文本回复，标记当前步完成。
        asst = ChatMessage("assistant", content)
        msgs.append(asst)
        if plan and idx < len(plan):
            plan = [dict(s) for s in plan]
            plan[idx] = {**plan[idx], "status": "done", "result": content}
        done(emitter, "call_model")
        return {"messages": msgs, "full_response": content, "plan": plan,
                "nodes_visited": visited}

    return call_model_node


def _merge_tool_delta(acc: dict, d: dict, emitter, idx: int) -> None:
    """把流式 tool_call_delta 按 index 合并进 acc，并 emit tool_call_start/args 帧。

    OpenAI 流式协议：同一 index 的 name 仅首片，arguments 逐片拼接。
    首片时 emit tool_call_start（前端占位终端卡片），每片 emit tool_call_args
    （前端逐字渲染参数）。
    """
    i = d.get("index", 0)
    if i not in acc:
        acc[i] = {"id": "", "function": {"name": "", "arguments": ""}}
        emit(emitter, {"type": "tool_call_start",
                        "name": d.get("function", {}).get("name", ""),
                        "step_index": idx})
    if d.get("id"):
        acc[i]["id"] = d["id"]
    fn = d.get("function", {})
    if fn.get("name"):
        acc[i]["function"]["name"] = fn["name"]
    args_inc = fn.get("arguments", "")
    if args_inc:
        acc[i]["function"]["arguments"] += args_inc
        emit(emitter, {"type": "tool_call_args", "text": args_inc, "step_index": idx})


def _finalize_acc(acc: dict, emitter) -> list[dict]:
    """流结束：把 acc 聚合成 OpenAI tool_calls 列表，并 emit tool_call 帧。"""
    from .base import display_mode_for
    tool_calls: list[dict] = []
    for i in sorted(acc):
        tc = acc[i]
        name = tc["function"]["name"]
        args_str = tc["function"]["arguments"] or "{}"
        try:
            args_obj = json.loads(args_str)
        except Exception:  # noqa: BLE001
            args_obj = {}
        tool_calls.append({
            "id": tc["id"] or f"call_{i}",
            "type": "function",
            "function": {"name": name, "arguments": args_str},
        })
        emit(emitter, {"type": "tool_call", "name": name, "input": args_obj,
                        "display_mode": display_mode_for(name)})
    return tool_calls
