"""Evaluation Harness：Agent 能力可量化度量与持续基准追踪。

构建系统化评估框架，设计多维度评测基准（准确性、鲁棒性、任务完成率、端到端延迟等），
实现自动化评测流水线，驱动数据飞轮式迭代优化。
"""
from __future__ import annotations

from .dataset import GoldenSet
from .judge import LLMJudge
from .metrics import Metrics, run_evaluation

__all__ = ["GoldenSet", "LLMJudge", "Metrics", "run_evaluation"]
