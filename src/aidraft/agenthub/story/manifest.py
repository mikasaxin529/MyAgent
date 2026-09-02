"""剧本分镜创作智能体 · 注册清单。

协议变量：AGENT_ID / DISPLAY_NAME / DESCRIPTION / IDENTITY_COLOR / PLACEHOLDER。
identity_color 用紫 #7C5CBF（与语文朱砂、通用蓝区分）。
"""
from __future__ import annotations

AGENT_ID = "story"
DISPLAY_NAME = "剧本分镜创作"
DESCRIPTION = "故事创意 → 剧本 + 分镜表 + 角色形象图（docx/xlsx/图片包/HTML）"
IDENTITY_COLOR = "#7C5CBF"
PLACEHOLDER = "描述你的故事创意，例如：一只迷路的小北极熊想回家，适合 6-8 岁儿童短片"
# 图自管 system 消息（extract_brief 插入 SYSTEM_EXTRACT_BRIEF），端点不注入
MANAGED_SYSTEM = False
