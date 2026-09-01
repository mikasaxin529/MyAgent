"""ToolNode：执行 assistant 的 tool_calls（自研，不引 LangChain ToolNode）。

遍历末条 assistant 消息的 tool_calls，调 registry.find_spec(name).func(**args)。
返回类型分流（对齐 SkillSpec.func 放宽为 Any）：
- dict → 取 content + search_items（websearch _tool_search 用）
- list → 当 search_items 集合，content 用 JSON 序列化
- str → 纯文本 ToolMessage content
工具结果包成 role="tool" 消息追加，并 emit tool_result + 逐条 search_item 帧
（对齐 ChatFlow formatters/web_search.py 逐条 search_item）。
"""
from __future__ import annotations

import asyncio
import json

from ...gateway import ChatMessage
from ..state import AgentGraphState
from .base import done, emit, visit


def make_tool_node(gateway, registry, emitter=None):
    async def tool_node(state: AgentGraphState) -> dict:
        visited = visit(state, "tools", emitter)
        msgs = list(state.get("messages") or [])
        tool_messages = list(state.get("tool_messages") or [])
        last = msgs[-1] if msgs else None
        # 兼容 dict（OpenAI）与 ChatMessage 两种形态读 tool_calls。
        if isinstance(last, dict):
            tcs = last.get("tool_calls") or []
        else:
            tcs = getattr(last, "tool_calls", None) or []
        if not tcs:
            done(emitter, "tools")
            return {"messages": msgs, "tool_messages": tool_messages,
                    "nodes_visited": visited}

        for tc in tcs:
            name = tc["function"]["name"]
            args_str = tc["function"].get("arguments", "{}")
            try:
                args = json.loads(args_str) if args_str else {}
            except Exception:  # noqa: BLE001
                args = {}
            spec = registry.find_spec(name) if registry else None
            if spec is None:
                content = f"[{name}] 工具未注册。"
                search_items: list = []
            else:
                try:
                    # SkillSpec.func 多为同步（Tavily/Open-Meteo 同步 HTTP），
                    # 用 to_thread 避免阻塞 event loop。
                    result = await asyncio.to_thread(spec.func, **args)
                except Exception as exc:  # noqa: BLE001
                    result = {"content": f"[{name}] 调用失败：{exc!r}",
                              "search_items": [], "error": True}
            if isinstance(result, dict):
                content = str(result.get("content", ""))
                search_items = result.get("search_items") or []
            elif isinstance(result, list):
                search_items = result
                content = json.dumps(result, ensure_ascii=False)
            else:
                content = str(result)
                search_items = []
            tool_msg = ChatMessage("tool", content, tool_call_id=tc["id"], name=name)
            msgs.append(tool_msg)
            tool_messages.append(tool_msg.to_dict())
            emit(emitter, {"type": "tool_result", "name": name,
                            "output": content[:2000], "search_items": search_items})
            for item in search_items:
                emit(emitter, {"type": "search_item", "name": name,
                                "url": item.get("url", ""),
                                "title": item.get("title", ""),
                                "snippet": item.get("snippet", "")})
        done(emitter, "tools")
        return {"messages": msgs, "tool_messages": tool_messages,
                "nodes_visited": visited}

    return tool_node
