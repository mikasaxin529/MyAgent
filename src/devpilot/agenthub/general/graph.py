"""通用对话智能体图：复用现有 ChatFlow 编排图（route→planner→call_model⇄tools→after_tool→reflector→save→memory）。

本图与语文智能体共用同一套接口契约（build_graph(gateway, registry, audit, emitter)），
以便注册中心统一装载。内部实现直接委托 graph.build_chat_graph。
"""
from __future__ import annotations

from typing import Any, Callable


def build_graph(
    gateway: Any,
    registry: Any,
    audit: Any | None = None,
    emitter: Callable[[dict], None] | None = None,
) -> Any:
    """组装并编译通用对话图。

    参数：
        gateway:  模型网关
        registry: Skill 注册中心
        audit:    审计日志
        emitter:  事件回调，节点把帧推给 web 层

    返回：
        langgraph 编译后的图，可 .astream(input) 异步流式执行。
    """
    from ...graph.graph import build_chat_graph

    return build_chat_graph(gateway, registry, audit=audit, emitter=emitter)