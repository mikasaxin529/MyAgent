"""审计日志：Agent 全行为可追溯。

Agent 行为审计 + 评估数据回流。

审计日志在 DevPilot 里承担三重角色（设计要点）：

    1. 可解释性（Explainability）：
       每次 LLM 调用、工具调用、审批决策、Agent 步骤都落一条记录，
       事后任意一问"Agent 当时为什么这么做"，都能从时间线还原——
       这是回答"AI 可不可信"的硬证据，而非口头保证。

    2. Evaluation Harness 的原始数据源：
       离线评估器把 audit 导出的 JSONL 作为"实际行为流"，与"期望行为流"
       对齐比对，计算工具选型准确率、步骤数、审批介入率等指标。
       没有审计，评估就无米下锅。

    3. 数据飞轮（人工标注回流）：
       把审计里"被人工改写参数的审批""被拒绝的步骤"挑出来，就是天然
       的纠偏样本；人工补标后回训，模型越用越准。审计是飞轮的"原料仓"。

格式约定：
    导出为 JSONL（每行一个独立 JSON 对象），原因：
      - 追加写友好（单行 append，无需重写整文件），适合流式审计。
      - 行级可解析，大文件可按行流读，不必整文件加载进内存。
      - 每行是一条 AuditEntry 的 dict 投影，字段固定，便于下游脚本消费。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class AuditEntry:
    """单条审计记录。

    一条记录 = "谁(actor) 在何时(timestamp) 做了什么(event)，细节如何(detail)"，
    并带 trace_id 贯穿同一次任务的所有事件，便于按"一次 Agent 运行"切片回放。

    属性：
        timestamp: ISO8601 时间戳（秒级精度），事件发生时刻。
        event:     事件类型，取值 "llm_call" | "tool_call" | "approval" | "agent_step"
                   等。类型即分类标签，看板与评估都按它聚合。
        actor:     行为主体，可能是某个 Agent（如 "coder_agent"）或人（"human"）。
        detail:    事件细节，结构因 event 而异（如 tool_call 里放工具名/参数/返回）。
        trace_id:  追踪 ID，同一次任务共享一个 trace_id，把它串成一条完整链路。
    """

    timestamp: str
    event: str          # "llm_call" | "tool_call" | "approval" | "agent_step"
    actor: str          # 哪个 Agent / 人
    detail: dict
    trace_id: str = ""


class AuditLog:
    """审计日志收集器。

    设计为进程内内存收集（list），简单可控；落地通过 `export()` 写成 JSONL。
    不直接接文件/DB 是为了解耦：收集与持久化分离，便于测试与替换存储后端。

    Agent 行为审计 + 评估数据回流。
    """

    def __init__(self) -> None:
        # 内存缓冲。所有 record() 都 append 到这里；export() 一次性 flush 到文件。
        # 测试场景下也可以直接 entries() 取出来断言，不必走文件 IO。
        self._entries: list[AuditEntry] = []

    def record(self, event: str, actor: str, detail: dict, trace_id: str = "") -> None:
        """记录一条审计事件。

        Agent 行为审计——每个关键动作都留痕。

        trace_id 透传说明（设计要点）：
            上游 AgentRunner 在一次任务开始时生成唯一 trace_id，逐层透传给
            LLM 调用、工具调用、审批门；本方法把它原样写进记录。这样事后
            用一个 trace_id 就能捞出"这次任务从头到尾发生了什么"，是
            可解释性与按任务评估的基础。

        参数：
            event:    事件类型。
            actor:    行为主体。
            detail:   事件细节 dict。
            trace_id: 追踪 ID，默认空串（兼容旧调用）；建议上游显式传入。
        """
        # 时间戳用秒级精度：审计关心"事件顺序与大致时刻"，秒级足够且更紧凑；
        # 若将来要定位毫秒级竞态，再把 timespec 调到 "milliseconds"。
        self._entries.append(
            AuditEntry(
                datetime.now().isoformat(timespec="seconds"),
                event,
                actor,
                detail,
                trace_id,
            )
        )

    def entries(self) -> list[AuditEntry]:
        """返回全部审计记录的副本。

        返回副本（list 浅拷贝）而非内部引用，避免外部误改内部状态；
        AuditEntry 本身是 dataclass，浅拷贝已足够防止增删条目。
        """
        return list(self._entries)

    def export(self, path: str) -> None:
        """把全部审计记录导出为 JSONL 文件。

        为 Evaluation Harness 与数据飞轮提供原始数据。

        JSONL（每行一个 JSON 对象）的好处见模块 docstring。
        落地流程：
            1. 用 pathlib 确保父目录存在（审计目录可能尚未创建）。
            2. 逐条把 AuditEntry 序列化成 dict 再 json.dumps 成一行，
               ensure_ascii=False 让中文 detail 正常落盘，便于人眼审阅。
            3. 行尾换行，符合 JSONL 规范（每行以 \\n 结束）。

        参数：
            path: 目标文件路径；父目录不存在会自动创建。
        """
        # pathlib 统一处理跨平台路径，且 mkdir(parents=True) 一步建好嵌套目录。
        target = Path(path)
        # exist_ok=True：目录已存在不报错；避免并发导出时竞态抛异常。
        target.parent.mkdir(parents=True, exist_ok=True)

        # 写文件用 utf-8 显式声明：Windows 默认编码可能是 GBK，会导致中文 detail
        # 写入失败或乱码——审计数据绝不能因编码问题损坏。
        with target.open("w", encoding="utf-8") as f:
            for entry in self._entries:
                # dataclass 实例没有内置 to_dict，用 asdict 转 dict 再序列化。
                # 这里用 dataclasses.asdict，避免手写 field 映射、降低维护成本。
                line = json.dumps(
                    _entry_to_dict(entry),
                    ensure_ascii=False,
                )
                # 每行一个 JSON + 换行，即标准 JSONL；下游可逐行 json.loads。
                f.write(line + "\n")

    def to_summary(self) -> dict[str, int]:
        """按 event 类型聚合计数，返回看板友好的统计 dict。

        让审计数据可直接喂给监控看板。

        典型输出：{"llm_call": 12, "tool_call": 7, "approval": 3, "agent_step": 5}
        用途：
            - 看板一眼看出"这次任务 LLM 调了多少次、工具用了几次、人介入几次"，
              人介入率（approval / tool_call）是衡量"自治程度"的关键指标。
            - 异常检测：某次 approval 激增可能意味着模型在危险地带反复试探。
            - 评估器据此快速算指标，无需加载全量 JSONL。

        返回：
            dict[event_type, count]；空日志返回 {}。
        """
        # 用普通 dict 手动累加，而非 collections.Counter，保持顶层零依赖、
        # 也便于在注释里讲清聚合逻辑（一行代码讲清"按 event 分组计数"）。
        summary: dict[str, int] = {}
        for entry in self._entries:
            # dict.get(..., 0) + 1 是最朴素的计数累加，语义直观。
            summary[entry.event] = summary.get(entry.event, 0) + 1
        return summary


# ---- 模块级辅助函数 ----


def _entry_to_dict(entry: AuditEntry) -> dict:
    """把 AuditEntry 转成可 JSON 序列化的 dict。

    放在模块级而非 dataclass 方法里，是因为 AuditEntry 是稳定的 dataclass，
    用 dataclasses.asdict 即可；这里包一层是为了集中"序列化策略"，
    将来若 detail 里出现不可直接序列化的对象（如 datetime），可在此统一处理。

    参数：
        entry: 一条审计记录。
    返回：
        字段齐全的 dict，可直接 json.dumps。
    """
    # 惰性导入：asdict 只在本函数被调用时才 import，顶层导入本模块零依赖。
    from dataclasses import asdict

    # asdict 会递归把 dataclass 转成 dict（含嵌套 dataclass），这里 AuditEntry
    # 字段都是基础类型，转出来即可直接 JSON 序列化。
    return asdict(entry)
