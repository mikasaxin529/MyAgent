"""Agent 运行时：记忆（Memory）。

历史说明：本模块原有手写 ReAct 循环（react.py）与 Planner（planner.py），
作为"先手写理解机制、再用 langgraph 框架重构"的教学对照实现——项目定位
从研发流程平台转向多智能体内容创作平台后已删除，能力由 graph/ 的
langgraph 编排层承接。现仅保留 Memory：Chat 端点用它做历史三段式压缩。
"""
from __future__ import annotations

from .memory import Memory

__all__ = ["Memory"]
