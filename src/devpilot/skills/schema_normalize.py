"""SkillSpec schema 规范化：把 skill 能力包成 OpenAI tools 格式。

原生 function-calling 要求 LLM 接受标准 OpenAI tools：
    {"type":"function","function":{"name","description","parameters":<JSON Schema>}}

DevPilot 的 SkillSpec.schema 期望是标准 JSON Schema
（{type:"object",properties,required}）。to_openai_tool 把单个 spec 包成
tools 元素；to_openai_tools 批量转，供 call_model 节点按 route 决定绑定哪些工具。

注意：repo/cicd/issue skill 现有 schema 非标准（如 {"job":{"type":"string"}}
而非 {"type":"object","properties":{"job":...}}）。新 SSE 图的 call_model
默认只绑 websearch/weather/fetch_page（已标准）；repo/cicd/issue 若要绑
call_model，需先在此处或 skill 内补全标准 JSON Schema（渐进迁移）。
"""
from __future__ import annotations

from .registry import SkillSpec


def to_openai_tool(spec: SkillSpec) -> dict:
    """把 SkillSpec 包成单个 OpenAI tool 元素。

    description 优先用 guidance（厚描述，给 LLM 何时用/返回什么的边界），
    退回 description（简短）。parameters 用 spec.schema（要求标准 JSON Schema）。
    """
    desc = spec.guidance or spec.description
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": desc,
            "parameters": spec.schema or {"type": "object", "properties": {}},
        },
    }


def to_openai_tools(specs: list[SkillSpec]) -> list[dict]:
    """批量转 SkillSpec → OpenAI tools 列表，供 stream_chat(tools=...) 透传。"""
    return [to_openai_tool(s) for s in specs]


def specs_by_names(specs: list[SkillSpec], names: list[str]) -> list[SkillSpec]:
    """按名筛选 specs（call_model 据 route 决定绑哪些工具）。"""
    wanted = set(names)
    return [s for s in specs if s.name in wanted]
