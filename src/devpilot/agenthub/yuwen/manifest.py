"""语文课件生成智能体 · 注册清单（契约 2.2 节）。

协议变量：AGENT_ID / DISPLAY_NAME / DESCRIPTION / IDENTITY_COLOR / PLACEHOLDER。
identity_color 用朱砂 #B5442E（对齐设计系统 --seal 色）。
"""
from __future__ import annotations

AGENT_ID = "yuwen"
DISPLAY_NAME = "语文课件生成"
DESCRIPTION = "课文名 + 年级 → pptx / HTML / docx 三件套"
IDENTITY_COLOR = "#B5442E"
PLACEHOLDER = "输入课文名+年级，例如：《乌鸦喝水》 二年级 识字课"
# 图自管 system 消息（extract_params 插入 SYSTEM_EXTRACT），端点不注入 SYSTEM_CHAT
MANAGED_SYSTEM = False
