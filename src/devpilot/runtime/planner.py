"""任务规划器：把用户需求拆解为可执行子任务序列。

实现 Planning 能力。

Plan-and-Execute vs ReAct 的取舍（设计要点）：
    ReAct（见 react.py）是"边推理边执行"，每一步都基于最新 Observation 决策，
    灵活但容易局部短视、步数发散。
    Plan-and-Execute 是"先规划再执行"：先用一轮 LLM 把任务拆成有序子任务列表，
    再逐个交给执行器（如 ReAct）完成。优势是全局视角、可并行/可审计/可重规划，
    代价是多一次 LLM 规划调用、且计划可能脱离实际执行反馈。

    本项目的取舍：
    - 复杂需求（多文件改动、多阶段）走 Plan-and-Execute：先 plan 拆解，每个子任务
      再丢给 ReAct 执行，兼顾全局规划与局部灵活。
    - 简单单步任务直接 ReAct，省掉规划开销。
    - 两者都通过同一 gateway 调用，模型可热切换。

W3 实现：用 gateway 的 json_mode 让 LLM 输出 {"steps": [...]}，解析返回 list[str]。
"""
from __future__ import annotations

import json

from ..gateway import ChatMessage


class Planner:
    """任务规划器：把自然语言需求转成结构化子任务列表。

    职责：在执行前先做全局规划，输出"可独立执行、有明确验收标准"
    的子任务序列。下游（如 ReAct 运行时）再逐个消费这些子任务。
    """

    def __init__(self, gateway) -> None:
        """构造函数。

        Args:
            gateway: 模型网关。规划调用走 gateway.chat(json_mode=True)，
                强制 LLM 输出合法 JSON，降低解析失败率。
        """
        self.gateway = gateway

    def plan(self, task: str) -> list[str]:
        """把自然语言任务拆成有序子任务列表。

        流程：
            1. 组装 system prompt：引导拆解为"可独立执行、有明确验收标准"的子任务
            2. gateway.chat(json_mode=True) 拿到 {"steps": [...]} 的 JSON
            3. 解析出 list[str]
            4. 任意环节失败 → 返回 [task] 兜底（不崩，让上层至少能跑原始任务）

        兜底设计（[task]）的考量：
            规划是"增强项"不是"必选项"。即使规划失败，把整个原始任务作为一个子任务
            返回，ReAct 仍能跑，只是少了拆解的好处。这保证 Planner 永远不会成为
            整条链路的单点故障。满足"稳定性"要求。

        Args:
            task: 用户原始需求（自然语言）。

        Returns:
            有序子任务列表。失败时返回单元素 [task]。
        """
        # ---- system prompt ----
        # 关键引导点：每个子任务"可独立执行 + 有验收标准"，避免拆出互相耦合、
        # 无法独立验收的伪子任务。这是 Plan-and-Execute 质量的核心。
        system = (
            "你是一个任务规划专家。把用户需求拆解为有序、可独立执行的子任务列表。\n"
            "拆解原则：\n"
            "1. 每个子任务可独立完成、有明确验收标准；\n"
            "2. 子任务之间有合理顺序，先基础后依赖；\n"
            "3. 粒度适中，既不过细（避免无意义拆分）也不过粗（避免无法独立验收）；\n"
            "4. 用简洁祈使句描述，如\"读取 config.py\"\"为 X 函数补单测\"。\n"
            "严格输出 JSON：{\"steps\": [\"子任务1\", \"子任务2\", ...]}，不要输出任何其他内容。"
        )

        try:
            # json_mode=True：让模型走 JSON 输出模式，gateway/providers 层会设置
            # response_format=json_object，从协议层保证输出是合法 JSON。
            resp = self.gateway.chat(
                [ChatMessage("system", system), ChatMessage("user", task)],
                temperature=0.3,  # 低温度：规划要稳定可复现，不要发散
                json_mode=True,
            )
            content = resp.content
        except Exception as e:
            # 模型不可用 / 网关 fallback 全失败：兜底返回原始任务，不崩。
            # 缺失时优雅降级而非崩溃。
            return [task]

        # ---- 解析 JSON ----
        # 即便开了 json_mode，仍可能有前后多余文本或转义问题，必须容错解析。
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # 极端情况：json_mode 没生效或模型仍带多余文本。尝试从文本里抠 JSON 段。
            data = _extract_json_object(content)

        if not isinstance(data, dict):
            return [task]

        steps = data.get("steps")
        # 校验：必须是 list 且元素都是 str。否则兜底。
        if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
            return [task]
        # 空列表兜底：模型可能返回 {"steps": []}，此时退化为原始任务。
        if not steps:
            return [task]

        return steps


def _extract_json_object(text: str) -> dict | None:
    """从可能带杂质的文本里抠出第一个 JSON 对象。

    json_mode 偶发失效（部分兼容服务端不支持 response_format）时，模型可能输出
    带前后说明文字的 JSON。这里用最朴素的方式：找第一个 '{' 到最后一个 '}'，
    尝试 json.loads；失败则返回 None，由上层兜底。

    不引入第三方 json5 / 复杂正则：惰性导入原则 + 90% 场景朴素实现够用。
    """
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None
