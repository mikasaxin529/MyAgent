"""语文智能体节点工厂集合。

每个节点一个模块（阶段 2 各自独立演进）：
- extract_params: 对话追问收集参数
- gen_content:    LLM 生成课件 JSON（反思重试）
- render:         调 render_all.py 渲染三件套
- report:         汇总交付清单，推终帧
"""
from __future__ import annotations

from .extract_params import _make_extract_params_node, _normalize_grade
from .gen_content import _make_gen_content_node
from .render import _make_render_node
from .report import _make_report_node

__all__ = [
    "_make_extract_params_node",
    "_normalize_grade",
    "_make_gen_content_node",
    "_make_render_node",
    "_make_report_node",
]
