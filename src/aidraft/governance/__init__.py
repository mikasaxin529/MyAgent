"""Human-on-the-Loop 治理层：审批门 / 审计 / 反馈回流。

设计 Human on the Loop 人机协同治理机制，
确保关键决策节点保留人工审核与干预能力，构建基于人工标注的能力持续升级闭环。
"""
from __future__ import annotations

from .approval import ApprovalGate, ApprovalRequest, ApprovalResult
from .audit import AuditLog, AuditEntry

__all__ = ["ApprovalGate", "ApprovalRequest", "ApprovalResult", "AuditLog", "AuditEntry"]
