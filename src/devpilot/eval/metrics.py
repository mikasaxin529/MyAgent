"""评测指标与自动化流水线。

多维度评测基准 + 自动化评测流水线 + 持续基准追踪。
指标：准确性、鲁棒性、任务完成率、端到端延迟、token 成本。

为什么用"多维度指标"而不是单一分？
----------------------------------------------------------------------
单一"准确率"分数无法回答工程上的关键追问：
- "准确但慢"能上生产吗？→ 需要 avg_latency_ms
- "准确但贵"划算吗？   → 需要 avg_token_cost
- "正常 case 对但对抗 case 全崩"稳健吗？→ 需要 robustness
- "整体分高但某类 tag 长期低分"哪里要补？→ 需要 per_tag
多维度指标把"模型质量"拆成可独立归因、可独立优化的信号，是
"数据飞轮"能够定向迭代的前提——你知道该往哪个方向投数据/改 prompt。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Metrics:
    """一次全量评测的聚合结果。

    字段说明（每个字段都对应一个可独立优化的质量维度）
    --------
    accuracy:               所有 case 的 overall 评分均值。整体质量基线。
    robustness:             对带 "edge"/"adversarial" tag 子集的 accuracy。
                            衡量模型在分布外/对抗输入下的退化程度。
                            无此类 case 时退化为 accuracy（避免指标空缺）。
    task_completion_rate:    passed=True 的 case 占比。业务侧最关心：
                            "有多少比例的任务是真正做完了的"。
    avg_latency_ms:         平均端到端延迟。SLA 视角关键指标。
    avg_token_cost:         平均 token 消耗。成本视角关键指标。
    per_tag:                {tag: {"accuracy": float, "count": int}}
                            按标签分维度统计，定位薄弱环节，驱动数据飞轮。
    """

    accuracy: float = 0.0
    robustness: float = 0.0
    task_completion_rate: float = 0.0
    avg_latency_ms: float = 0.0
    avg_token_cost: float = 0.0
    per_tag: dict = field(default_factory=dict)  # 按标签分维度统计

    def to_dict(self) -> dict:
        """序列化为可写 JSON 的 dict，便于持久化到历史基准库做回归对比。"""
        return {
            "accuracy": self.accuracy,
            "robustness": self.robustness,
            "task_completion_rate": self.task_completion_rate,
            "avg_latency_ms": self.avg_latency_ms,
            "avg_token_cost": self.avg_token_cost,
            "per_tag": self.per_tag,
        }


# ----------------------------------------------------------------------
# 视为"鲁棒性"评测的 tag 集合
# ----------------------------------------------------------------------
# 这些 tag 标识"非标准/对抗性"输入：边界条件、对抗 prompt、噪声等。
# robustness 只在这部分 case 上算 accuracy，反映模型在分布外的退化。
# 用集合做成员判断，O(1) 且可扩展。
ROBUSTNESS_TAGS = frozenset({"edge", "adversarial"})


def run_evaluation(golden_set, judge, agent_run_fn) -> Metrics:
    """跑全量评测集，汇总多维度指标。

    自动化评测流水线。

    参数
    ----
    golden_set:    GoldenSet 实例，提供 .cases() -> list[GoldenCase]。
                   每个 GoldenCase 有 id/task/expected/rubric/tags。
    judge:         LLMJudge 实例，提供 .judge(task,actual,expected,rubric)->dict。
    agent_run_fn:  callable(task: str) -> (output: str, latency_ms: float,
                   tokens: int)。被测 Agent 的运行入口；把"跑 Agent"抽象成
                   callable，使得评测与 Agent 的具体实现解耦——
                   本地 ReAct Agent、远端 HTTP 服务都能套同一评测流水线。

    返回
    ----
    Metrics：聚合后的多维度指标。

    自动化流水线闭环（设计要点）
    --------
    本函数是"自动化评测流水线"的核心，但它不是一次性的——它处在
    数据飞轮的闭环中：

        跑评测(run_evaluation)
          → 得到 Metrics + 失败 case 明细
          → 人工复核失败 case，判断是真 bug 还是 golden 有误
          → 真 bug：修 Agent / 加回流样本到 GoldenSet.add()
          → golden 有误：修正 expected
          → 重新跑评测，对比历史 Metrics（回归基准）
          → 指标不退步才允许发布 → 拦截回归

    回归基准怎么做？
    ——把每次 run_evaluation 的 Metrics.to_dict() 保存为历史快照
    （如 evals/history/2024-01-01.json），发布前对比当前 Metrics 与
    上一版：任一维度退步超阈值即报警。本函数只产 Metrics，对比逻辑
    放在调用方（CI 门槛）即可，保持单一职责。
    """
    cases = golden_set.cases()
    if not cases:
        # 空集：直接返回零值 Metrics，不抛错——评测流水线对空集要幂等。
        return Metrics()

    # ---- 逐 case 累计容器 ----
    # 用列表收集每个 case 的细粒度结果，再一次性聚合。
    # 这样既可算总量均值，也便于后续按 tag 分桶。
    records: list[dict] = []

    for case in cases:
        # ---- 1. 跑被测 Agent ----
        # 异常隔离：单条 case 跑挂不能让全量评测中断。
        # Agent 崩溃也算一种"质量信号"——记 0 分 + 高延迟，让指标如实反映。
        try:
            output, latency_ms, tokens = agent_run_fn(case.task)
            # 防御性兜底：callable 可能返回非预期类型
            output = output if isinstance(output, str) else str(output)
            latency_ms = float(latency_ms) if latency_ms is not None else 0.0
            tokens = int(tokens) if tokens is not None else 0
        except Exception as e:  # noqa: BLE001 - 评测要"全量跑完"，单点失败记 0 分
            output = f"<agent_run_fn raised: {e!r}>"
            latency_ms = 0.0
            tokens = 0

        # ---- 2. 用 LLM-judge 打分 ----
        # judge 内部已做异常降级，这里直接拿 dict。
        verdict = judge.judge(
            task=case.task,
            actual=output,
            expected=case.expected,
            rubric=list(case.rubric),
        )
        overall = float(verdict.get("overall", 0.0))
        passed = bool(verdict.get("passed", False))

        records.append({
            "overall": overall,
            "passed": passed,
            "latency_ms": latency_ms,
            "tokens": tokens,
            "tags": list(case.tags) if case.tags else [],
        })

    # ---- 3. 全局聚合 ----
    n = len(records)
    total_overall = sum(r["overall"] for r in records)
    total_passed = sum(1 for r in records if r["passed"])
    total_latency = sum(r["latency_ms"] for r in records)
    total_tokens = sum(r["tokens"] for r in records)

    accuracy = total_overall / n
    task_completion_rate = total_passed / n
    avg_latency_ms = total_latency / n
    avg_token_cost = total_tokens / n

    # ---- 4. 鲁棒性子集：只在 edge/adversarial tag 上算 accuracy ----
    robust_records = [r for r in records if ROBUSTNESS_TAGS & set(r["tags"])]
    if robust_records:
        robustness = sum(r["overall"] for r in robust_records) / len(robust_records)
    else:
        # 无对抗样本时退化为整体 accuracy——避免指标空缺造成"无信号"假象。
        robustness = accuracy

    # ---- 5. 按 tag 聚合 per_tag ----
    # {tag: {"accuracy": 该 tag 下 overall 均值, "count": 该 tag 下 case 数}}
    # 这是"数据飞轮定向迭代"的关键信号源：哪个 tag 长期低分，
    # 就优先往哪个方向回流标注样本/改 Agent prompt。
    tag_acc_sum: dict[str, float] = {}
    tag_count: dict[str, int] = {}
    for r in records:
        for tag in r["tags"]:
            tag_acc_sum[tag] = tag_acc_sum.get(tag, 0.0) + r["overall"]
            tag_count[tag] = tag_count.get(tag, 0) + 1

    per_tag: dict[str, dict] = {}
    for tag, cnt in tag_count.items():
        per_tag[tag] = {
            "accuracy": tag_acc_sum[tag] / cnt if cnt else 0.0,
            "count": cnt,
        }

    return Metrics(
        accuracy=accuracy,
        robustness=robustness,
        task_completion_rate=task_completion_rate,
        avg_latency_ms=avg_latency_ms,
        avg_token_cost=avg_token_cost,
        per_tag=per_tag,
    )
