"""装配工厂：把 gateway/registry/audit/approval/orchestrator 串成一个可运行整体。

CLI（app.py）与 Web API（api.py）各自装配会重复代码，这里抽出公共工厂，
保证"单一事实源"——两边装配出的 Orchestrator 行为一致。

设计：
- build_registry()：注册 Repo/CICD/Issue Skill，凭证从环境变量读、缺失降级。
- build_runtime()：一站式装配，返回 Orchestrator 及其依赖，便于 API 层按需注入
  可观测审计（ObservableAuditLog）与 Web 审批门（WebApprovalGate）。
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


def build_runtime(audit=None, approval=None):
    """一站式装配：返回 (gateway, registry, audit, approval, orchestrator)。

    参数：
        audit:    可选，注入自定义 AuditLog（如 ObservableAuditLog 用于 WS 推流）。
                  默认 new 一个普通 AuditLog。
        approval: 可选，注入自定义 ApprovalGate（如 WebApprovalGate 用于前端审批）。
                  默认 new 一个 CLI ApprovalGate。

    返回：
        (gateway, registry, audit, approval, orchestrator)
    """
    from ..gateway import build_default_gateway
    from ..governance.audit import AuditLog
    from ..governance.approval import ApprovalGate
    from ..agents.orchestrator import Orchestrator

    gw = build_default_gateway()
    registry = build_registry()
    if audit is None:
        audit = AuditLog()
    if approval is None:
        approval = ApprovalGate()
    orchestrator = Orchestrator(gw, registry, audit=audit, approval=approval)
    return gw, registry, audit, approval, orchestrator
