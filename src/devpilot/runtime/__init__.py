"""Agent 运行时：Planning / ReAct / Tool Use / 记忆。

设计可扩展、易维护的 Agent 运行时架构，实现任务规划、推理决策、
工具调用、多 Agent 协作等核心能力。

设计原则（重要）：
- 本模块**手写** ReAct 循环，不直接套 LangChain，以完全掌控运行时行为
- 先定义抽象（Tool、AgentRuntime），再填实现，便于逐周增量
- 运行时只依赖 gateway 抽象，不感知具体模型
"""
from __future__ import annotations

from .types import Tool, ToolCall, AgentStep, AgentState
from .react import ReActRuntime
from .planner import Planner
from .memory import Memory

__all__ = [
    "Tool",
    "ToolCall",
    "AgentStep",
    "AgentState",
    "ReActRuntime",
    "Planner",
    "Memory",
]
