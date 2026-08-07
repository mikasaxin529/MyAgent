"""Agent 运行时核心类型定义。

Tool 抽象是 Skill 体系的基础：每个 MCP Skill 最终都会暴露成 Tool 供运行时调度。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol


@dataclass
class Tool:
    """一个可被 Agent 调用的工具。

    name/description 用于组装进 LLM 的 function-calling schema；
    func 是实际执行函数；schema 是 OpenAI 工具的 JSON Schema。
    """
    name: str
    description: str
    func: Callable[..., str]
    schema: dict = field(default_factory=dict)


@dataclass
class ToolCall:
    """Agent 决定调用某个工具的请求。"""
    name: str
    arguments: dict
    thought: str = ""


@dataclass
class AgentStep:
    """运行时单步：一次 think 或一次 act 的记录，用于审计与评估。"""
    kind: str  # "thought" | "action" | "observation"
    content: str
    tool: str | None = None


@dataclass
class AgentState:
    """Agent 运行状态：步数、已完成步骤、当前计划、上下文记忆。"""
    steps: list[AgentStep] = field(default_factory=list)
    plan: list[str] = field(default_factory=list)
    max_steps: int = 10
    finished: bool = False

    def step_count(self) -> int:
        return len(self.steps)


class AgentRuntime(Protocol):
    """运行时协议：接收任务，产出结果与执行轨迹。"""

    def run(self, task: str, tools: list[Tool] | None = None) -> tuple[str, AgentState]:
        ...
