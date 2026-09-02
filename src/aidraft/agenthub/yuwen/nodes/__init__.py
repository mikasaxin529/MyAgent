"""语文智能体节点工厂集合（阶段 2a：多阶段管线）。

每个节点一个模块：
- extract_params: 对话追问收集参数
- research:       大纲前联网资料搜索（可选增强，无 key 跳过，M2）
- gen_outline:    生成页面大纲（→ END 等确认）
- confirm:        查盘恢复大纲，确认/切主题/改纲
- gen_slides:     逐页生成内容（页级反思重试，重写自 gen_content）
- gen_plan:       教案 + 分层学习单
- review:         AI 审查评分（结构预检 + 内容抽查）
- revise:         按审查问题清单单页修订
- gen_images:     AI 配图回填（可选增强）
- render:         调 render_all.py 渲染三件套
- visual_review:  渲染后视觉审查（PPTX→逐页图→qwen-vl，可选增强）
- visual_fix:     视觉审查修复闭环（重生成问题页→重渲染→复查，降分回滚）
- report:         汇总交付清单，推终帧

页级工具（单页校验 / meta 合成 / LLM 调用签名适配）在 _page.py。
"""
from __future__ import annotations

from .confirm import _make_confirm_node
from .extract_params import _make_extract_params_node, _normalize_grade
from .gen_images import _make_gen_images_node
from .gen_outline import _make_gen_outline_node
from .research import _make_research_node
from .gen_plan import _make_gen_plan_node
from .gen_slides import _make_gen_slides_node
from .render import _make_render_node
from .report import _make_report_node
from .review import _make_review_node
from .revise import _make_revise_node
from .visual_fix import _make_visual_fix_node
from .visual_review import _make_visual_review_node

__all__ = [
    "_make_extract_params_node",
    "_normalize_grade",
    "_make_research_node",
    "_make_gen_outline_node",
    "_make_confirm_node",
    "_make_gen_slides_node",
    "_make_gen_plan_node",
    "_make_review_node",
    "_make_revise_node",
    "_make_gen_images_node",
    "_make_render_node",
    "_make_visual_review_node",
    "_make_visual_fix_node",
    "_make_report_node",
]
