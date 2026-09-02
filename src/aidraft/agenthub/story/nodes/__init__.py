"""剧本智能体节点工厂集合。

每个节点一个模块：
- extract_brief:      对话收集故事创意参数
- gen_synopsis:       生成梗概（→ END 等确认，第一确认点）
- confirm_synopsis:   查盘恢复梗概，确认/修改（跨轮状态机第二环）
- gen_characters:     设计角色卡（第二确认点产物）
- confirm_characters: 角色确认（确认后生成立绘再进分镜）
- gen_portraits:      角色标准立绘生图（可选增强，无 key 跳过）
- gen_storyboard:     创作分镜脚本（第三确认点产物）
- confirm_storyboard: 分镜确认（终确认 → export）
- export:             导出 docx/xlsx/html 交付物
- report:             汇总交付清单，推终帧
"""
from __future__ import annotations

from .confirm_characters import _make_confirm_characters_node
from .confirm_storyboard import _make_confirm_storyboard_node
from .confirm_synopsis import _make_confirm_synopsis_node
from .export import _make_export_node
from .extract_brief import _make_extract_brief_node
from .gen_characters import _make_gen_characters_node
from .gen_portraits import _make_gen_portraits_node
from .gen_storyboard import _make_gen_storyboard_node
from .gen_synopsis import _make_gen_synopsis_node
from .report import _make_report_node

__all__ = [
    "_make_extract_brief_node",
    "_make_gen_synopsis_node",
    "_make_confirm_synopsis_node",
    "_make_gen_characters_node",
    "_make_confirm_characters_node",
    "_make_gen_portraits_node",
    "_make_gen_storyboard_node",
    "_make_confirm_storyboard_node",
    "_make_export_node",
    "_make_report_node",
]
