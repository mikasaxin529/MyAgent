"""语文课件生成智能体（yuwen）。

课文名 + 年级 → pptx / HTML / docx 三件套。
图组装见 graph.py（阶段 1：extract_params → gen_content → render → report；
目标管线见其 docstring），清单见 manifest.py，节点实现在 nodes/，
共享状态与提示词见 state.py / prompts.py，渲染脚本见 scripts/，
参考契约见 references/。
"""
from __future__ import annotations

from .manifest import (
    AGENT_ID,
    DESCRIPTION,
    DISPLAY_NAME,
    IDENTITY_COLOR,
    PLACEHOLDER,
)

__all__ = [
    "AGENT_ID",
    "DISPLAY_NAME",
    "DESCRIPTION",
    "IDENTITY_COLOR",
    "PLACEHOLDER",
]
