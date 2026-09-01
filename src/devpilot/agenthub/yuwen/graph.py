"""语文智能体图组装（阶段 1 骨架）。

目标管线（阶段 2 逐节点实现）：
  extract_params（对话追问收集参数）
  → gen_outline（生成大纲，待实现）
  → 用户确认大纲（待实现）
  → gen_slides（逐页生成内容，待实现）
  → review（AI 审查，待实现）
  → revise（按审查意见修订，待实现）
  → gen_images（AI 生图，待实现）
  → render（渲染 pptx/html/docx 三件套）
  → report（交付汇总）

当前仍为阶段 1 之前的 4 节点行为（extract_params → gen_content → render →
report），节点实现拆分至 nodes/ 子包，共享状态与基础设施在 state.py，
提示词在 prompts.py。本模块只做图组装与条件边。
"""
from __future__ import annotations

from typing import Any, Callable

from langgraph.graph import END, StateGraph

from .nodes import (
    _make_extract_params_node,
    _make_gen_content_node,
    _make_render_node,
    _make_report_node,
)
from .state import YuwenState


# ---------------------------------------------------------------------------
# 条件边：_params_ready
# ---------------------------------------------------------------------------

def _params_ready(state: YuwenState) -> str:
    """条件边：参数齐备 → gen_content；否则 → END。"""
    if state.get("yuwen_params_ready"):
        return "gen_content"
    return "__end__"


# ---------------------------------------------------------------------------
# 图组装
# ---------------------------------------------------------------------------

def build_graph(
    gateway: Any,
    registry: Any,
    audit: Any | None = None,
    emitter: Callable[[dict], None] | None = None,
) -> Any:
    """组装并编译语文智能体 langgraph 图。

    参数：
        gateway:  模型网关（gateway.chat / gateway.stream_chat）
        registry: Skill 注册中心（本图暂不使用）
        audit:    审计日志（可选）
        emitter:  事件回调，节点把帧推给 web 层

    返回：
        langgraph 编译后的图，可 .astream(input) 异步流式执行。
    """
    graph = StateGraph(YuwenState)

    # 注册节点
    graph.add_node("extract_params", _make_extract_params_node(gateway, emitter))
    graph.add_node("gen_content", _make_gen_content_node(gateway, emitter))
    graph.add_node("render", _make_render_node(emitter))
    graph.add_node("report", _make_report_node(emitter))

    # 入口
    graph.set_entry_point("extract_params")

    # extract_params 条件出边：参数齐备 → gen_content；否则 → END
    graph.add_conditional_edges(
        "extract_params",
        _params_ready,
        {
            "gen_content": "gen_content",
            "__end__": END,
        },
    )

    # 主链
    graph.add_edge("gen_content", "render")
    graph.add_edge("render", "report")
    graph.add_edge("report", END)

    return graph.compile()
