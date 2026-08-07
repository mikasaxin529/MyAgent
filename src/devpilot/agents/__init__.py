"""Multi-Agent 编排：Planner / Coder / Reviewer / Tester 协作。

从 0 到 1 搭建 AI Agent 架构：多 Agent 协作能力落地。

拓扑选型：Orchestrator-Worker
- Orchestrator 负责任务分解、委派、结果聚合
- Worker 角色各司其职，通过共享黑板(Scratchpad)与状态通信
"""
from __future__ import annotations

from .orchestrator import Orchestrator
from .agents import CoderAgent, ReviewerAgent, TesterAgent

__all__ = ["Orchestrator", "CoderAgent", "ReviewerAgent", "TesterAgent"]
