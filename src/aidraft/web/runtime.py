"""装配工厂：为 Web API / CLI 构造 SkillRegistry。

设计：
- build_registry()：注册 Repo/CICD/Issue/WebSearch/Weather 五个 Skill，
  凭证从环境变量读、缺失降级——general 智能体的 langgraph 图经 registry
  发现并调度这些工具能力。

历史说明：原有 build_runtime()（装配 gateway/audit/approval/orchestrator
的研发流程 Orchestrator）随项目定位转向已删除；聊天主链路的运行时装配
在 web/api.py 的 chat_sse 端点内完成（gateway + registry + audit + 图）。
"""
from __future__ import annotations

import os


def build_registry():
    """构造默认 SkillRegistry，注册 Repo/CICD/Issue/WebSearch/Weather 五个 Skill。

    凭证策略：一律从环境变量读，缺失优雅降级（Skill 构造不抛错，调用时降级）。
    WeatherSkill 用 Open-Meteo，免费无需凭证。
    """
    from ..skills.registry import SkillRegistry
    from ..skills.repo_skill import RepoSkill
    from ..skills.cicd_skill import CICDSkill
    from ..skills.issue_skill import IssueSkill
    from ..skills.websearch_skill import WebSearchSkill
    from ..skills.weather_skill import WeatherSkill

    registry = SkillRegistry()
    # RepoSkill：repo_path 用当前工作目录，便于在仓库根目录 demo。
    registry.register(RepoSkill(repo_path=os.getcwd()))
    # CICD/Issue Skill：构造时不传参，内部从环境变量读凭证。
    registry.register(CICDSkill())
    registry.register(IssueSkill())
    # WebSearch Skill：从环境变量读 TAVILY_API_KEY，缺失降级返回提示。
    registry.register(WebSearchSkill())
    # Weather Skill：Open-Meteo 免费无 key，直接可用。
    registry.register(WeatherSkill())
    return registry
