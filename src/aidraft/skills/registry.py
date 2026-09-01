"""Skill 注册中心与基类。

低代码 Skill 框架与可复用技能生态。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


@dataclass
class SkillSpec:
    """Skill 暴露的一个能力（对应 MCP tool / OpenAI function）。

    guidance 是给 LLM 的"厚描述"：何时用/何时不用/返回什么/看到什么优先采纳，
    注入 planner 的 TOOL_CATALOG 帮模型从描述自决选工具（学 ChatFlow 的 GUIDANCE）。
    description 是简短一句话（供简单展示）；guidance 是详细使用边界。

    func 返回类型放宽为 Any：原生 function-calling 下，ToolNode 据返回类型分流——
    str 当纯文本 ToolMessage；dict 取 content + search_items/fetch_status 等结构化
    字段（websearch 逐条 search_item 用）；list 当 search_items 集合。
    schema 必须是标准 JSON Schema（{type:"object",properties,required}），
    schema_normalize.to_openai_tool 会包成 OpenAI tools 格式喂 LLM。
    """
    name: str
    description: str
    func: Callable[..., Any]
    schema: dict
    guidance: str = ""


class Skill(Protocol):
    """Skill 抽象：每个内部系统封装成一个 Skill，暴露若干能力。"""
    name: str

    def specs(self) -> list[SkillSpec]: ...


class SkillRegistry:
    """Skill 注册中心：统一发现、调度、审计 Skill 调用。"""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def list_skills(self) -> list[str]:
        return list(self._skills)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def all_specs(self) -> list[SkillSpec]:
        """聚合所有 Skill 的能力清单，供 Agent 运行时组装 tools。"""
        specs: list[SkillSpec] = []
        for skill in self._skills.values():
            specs.extend(skill.specs())
        return specs

    def find_spec(self, spec_name: str) -> SkillSpec | None:
        """按能力名（SkillSpec.name）查找，供 executor 据 step.tool 动态调度。

        与 get(name) 区别：get 按 Skill 名（如 "weather"），find_spec 按
        具体能力名（如 "weather_forecast"）——一个 Skill 可能暴露多个能力。
        """
        for spec in self.all_specs():
            if spec.name == spec_name:
                return spec
        return None
