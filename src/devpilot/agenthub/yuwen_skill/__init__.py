"""语文课件生成智能体（yuwen_skill）。

课文名 + 年级 → pptx / HTML / docx 三件套。
图见 graph.py（extract_params → gen_content → render → report），
清单见 manifest.py，渲染脚本见 scripts/，参考契约见 references/。
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
