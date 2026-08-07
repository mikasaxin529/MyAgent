"""LLM-as-judge：用 LLM 对 Agent 输出打分。

自动化评测流水线。
设计要点：
- 用 rubric 引导 judge 分维度评分，减少主观漂移
- judge 模型与被测 Agent 模型不同，避免自评偏袒
- 多次/多模型投票，抑制单 judge 偏差

为什么需要 LLM-judge？
----------------------------------------------------------------------
传统评测只能做"字面匹配"（exact match / BLEU），但 Agent 输出往往是
自然语言+工具调用序列，正确答案可能有多种等价表达。用另一个 LLM 当
"阅卷老师"，能按语义判断"是否完成预期任务"，这是单一规则打分做不到的。
而 rubric（评分维度清单）把"主观判断"约束成"逐条核对"，极大降低
LLM 评审固有的主观漂移问题。
"""
from __future__ import annotations

from typing import Any


class LLMJudge:
    """LLM 评审器：用独立 LLM 按 rubric 给 Agent 输出打分。

    设计原则（设计要点）
    --------
    1. judge 模型应与被测 Agent 模型不同（甚至不同厂商）。
       原因：同一模型自评会有"自我偏好(self-preference)"偏袒——
       GPT-4 给 GPT-4 的输出打分系统性偏高。跨模型评审能消除这一偏差。
       工程上：被测 Agent 用 DeepSeek，judge 用 Qwen/GPT，互相独立。
    2. rubric（显式评分维度）是降低主观漂移的关键。
       不给 rubric 时，LLM 容易被"看起来流畅、措辞华丽"的输出带偏，
       给高分；给 rubric 后变成"逐项 yes/no 核对"，更接近规则评分。
    3. 可扩展为多 judge 投票：实例化多个 LLMJudge（不同 gateway），
       对同一 (task,actual,expected) 各打一次分，取 overall 均值/中位数，
       可进一步抑制单 judge 偶发偏差。本类的 judge() 是单 judge 版本，
       投票逻辑放在调用方聚合即可。
    """

    def __init__(self, gateway) -> None:
        """绑定一个模型网关。

        gateway: devpilot.gateway.Gateway 实例。
        为什么传 gateway 而不是直接传模型名？
        ——网关内部已封装了路由/fallback/限流/缓存，judge 复用同一套容错
        机制，provider 挂了会自动切备模型，评审不因单点故障而中断。
        """
        self.gateway = gateway

    # ------------------------------------------------------------------
    # system prompt：塑造"严格评审"人设
    # ------------------------------------------------------------------
    # 这段 system prompt 是降低主观漂移的核心手段之一。要点：
    # (a) 明确身份=严格评审，不是助手——禁止"鼓励性给分"。
    # (b) 只看 expected 与 rubric 是否被满足，忽略措辞华丽度——
    #     这是抑制 LLM 被"流畅度"带偏最关键的一条。
    # (c) 不存在的能力直接 0 分——避免"看起来像就给分"的幻觉。
    # (d) 输出强制 JSON，便于程序解析与聚合。
    SYSTEM_PROMPT = (
        "你是一名极其严格的代码评审专家，正在评估一个 AI Agent 的任务输出质量。\n"
        "你的职责：判断 Agent 的输出(actual)是否真正满足了任务(task)的预期结果(expected)，"
        "并按评分维度(rubric)逐项打分。\n\n"
        "评审准则——必须严格遵守：\n"
        "1. 只依据 expected 与 rubric 是否被满足来打分；不要被输出措辞是否华丽、"
        "是否冗长、是否礼貌所影响。流畅不等于正确。\n"
        "2. Agent 实际未展现出的能力，对应维度直接给 0 分；不要因'看起来像'或'可能做到'而给分。\n"
        "3. 每个维度评分在 0.0~1.0 之间：1.0=完全满足，0.5=部分满足，0.0=未满足。\n"
        "4. overall 是各维度分数的加权综合（可按等权平均），反映整体完成度。\n"
        "5. passed=true 当且仅当 overall 达到 0.7 及以上，且没有任一维度为 0。\n"
        "6. reason 用简短中文给出扣分理由，逐维度说明，不要泛泛而谈。\n\n"
        "你必须只输出一个合法 JSON 对象，不要输出任何其他文字、Markdown 或解释。"
    )

    def judge(self, task: str, actual: str, expected: str, rubric: list[str]) -> dict:
        """对一次 Agent 输出按 rubric 评分，返回结构化评分。

        自动化评测流水线（评审环节）。

        参数
        ----
        task:     原始任务描述（给 Agent 的输入）
        actual:   Agent 的实际输出（被评对象）
        expected: 期望结果/关键断言（ground truth，人工标注）
        rubric:   评分维度列表，如 ["正确修改了目标文件", "未引入语法错误",
                  "测试通过"]。每条对应 scores 里的一个键。

        返回
        ----
        dict: {
            "scores": {维度: 0~1},   # 分维度分数
            "overall": 0~1,          # 综合分
            "reason": str,            # 扣分理由
            "passed": bool            # 是否通过(>=0.7 且无 0 分维度)
        }
        解析失败时返回降级结果 {"overall":0.0, "reason":"judge解析失败",
        "passed":False}，保证评测流水线不会因单条 case 评审异常而整体崩溃。

        为什么用 json_mode？
        ——LLM 输出是自由文本，不强制格式就要写一堆正则/解析逻辑去抠数字，
        既脆弱又难维护。json_mode 让模型直出结构化 JSON，评审结果可被
        metrics 流水线直接聚合，是"自动化评测"的基础。
        """
        # ---- 1. 组装 user prompt：把 task/actual/expected/rubric 拼成评审材料 ----
        # rubric 用带编号的列表呈现，便于 judge 逐条核对并按相同键名输出 scores。
        rubric_text = "\n".join(
            f"{i + 1}. {item}" for i, item in enumerate(rubric)
        ) if rubric else "（本条 case 未提供 rubric，请按 expected 是否满足整体打分）"

        user_prompt = (
            "请按以下材料评审 Agent 的输出。\n\n"
            f"【任务 task】\n{task}\n\n"
            f"【Agent 实际输出 actual】\n{actual}\n\n"
            f"【期望结果 expected】\n{expected}\n\n"
            f"【评分维度 rubric】\n{rubric_text}\n\n"
            "请严格按 system 的评审准则打分，并输出如下 JSON（且只输出该 JSON）：\n"
            '{"scores": {"<维度原文1>": 0.0~1.0, "<维度原文2>": 0.0~1.0, ...}, '
            '"overall": 0.0~1.0, "reason": "逐维度扣分理由", "passed": true/false}'
        )

        # ---- 2. 调 LLM（json_mode 强制结构化输出）----
        # 走 gateway 而非裸调 provider：复用 fallback/限流/缓存。
        # temperature 用低值（0.0）以追求评审可复现——评审不需要"创造性"。
        try:
            from ..gateway.base import ChatMessage  # 惰性导入，避免顶层耦合

            messages = [
                ChatMessage("system", self.SYSTEM_PROMPT),
                ChatMessage("user", user_prompt),
            ]
            resp = self.gateway.chat(messages, temperature=0.0, json_mode=True)
            raw = resp.content
        except Exception as e:  # noqa: BLE001 - 评审环节不能让整体流水线崩
            # gateway 不可用（如未配 API Key / 限流打满）时优雅降级：
            # 返回 0 分 + 明确原因，下游 metrics 仍可继续聚合。
            return {
                "scores": {},
                "overall": 0.0,
                "reason": f"judge 调用失败: {e!r}",
                "passed": False,
            }

        # ---- 3. 解析 JSON ----
        parsed = self._parse_json(raw)
        if parsed is None:
            # 模型未遵守 json_mode（少数情况下模型仍会包裹 ```json 或加说明文字）。
            # 降级返回，保证流水线健壮：宁可记 0 分 + 标记失败，也不抛异常中断全量评测。
            return {
                "scores": {},
                "overall": 0.0,
                "reason": f"judge 解析失败，原始输出: {raw[:200]}",
                "passed": False,
            }

        # ---- 4. 规整字段，兜底缺失键 ----
        scores = parsed.get("scores") or {}
        # 强制 scores 的值为 float，避免字符串/None 污染下游聚合
        if isinstance(scores, dict):
            scores = {k: _clamp_float(v) for k, v in scores.items()}
        else:
            scores = {}

        overall = _clamp_float(parsed.get("overall"))
        # 若模型没给 overall，用各维度均值兜底——保证 overall 永远有值
        if overall == 0.0 and scores:
            try:
                overall = sum(scores.values()) / len(scores)
            except ZeroDivisionError:
                pass

        reason = str(parsed.get("reason", "")).strip() or "未给出理由"
        passed = bool(parsed.get("passed", overall >= 0.7 and all(v > 0 for v in scores.values())))

        return {
            "scores": scores,
            "overall": overall,
            "reason": reason,
            "passed": passed,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_json(raw: str) -> dict | None:
        """尽力从模型输出里抠出 JSON 对象，兼容常见脏输出。

        为什么单独抽这个方法？
        ——即便 provider 声称支持 json_mode，不同厂商兑现程度不一：
        有的会前后加 ```json 围栏，有的会附带一句"结果是："。逐层剥离
        后再用标准库 json 解析，最大化跨 provider 的健壮性。

        返回解析后的 dict；解析失败返回 None（由调用方降级处理）。
        """
        import json  # 惰性导入：顶层不依赖 json

        if not raw:
            return None

        text = raw.strip()

        # 剥离 ```json ... ``` 围栏
        if text.startswith("```"):
            # 去掉首行围栏
            text = text.split("```", 2)
            # text 形如 ['', 'json\n{...}\n', '...'] 或 ['', '{...}', '...']
            if len(text) >= 2:
                inner = text[1]
                # 去掉可能的 "json" 语言标记
                if inner.startswith("json"):
                    inner = inner[4:]
                text = inner.strip()
            else:
                text = raw.strip()

        # 截取第一个 { 到最后一个 }，处理模型在 JSON 前后加废话的情况
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]

        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except Exception:  # noqa: BLE001 - 解析失败交给调用方降级
            return None
        return None


# ----------------------------------------------------------------------
# 模块级工具函数
# ----------------------------------------------------------------------
def _clamp_float(v: Any, lo: float = 0.0, hi: float = 1.0) -> float:
    """把任意值规整为 [lo, hi] 区间的 float，非法值回落为 0.0。

    评审分必须在 0~1 之间；模型偶尔会输出 1.2、-0.1、"0.8" 之类，
    统一在这里 clamp，避免下游 metrics 出现非法数值。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if f < lo:
        return lo
    if f > hi:
        return hi
    return f
