"""MCP Skill 生态：把内部系统（代码仓库、CI/CD、项目管理）封装为标准化 Skill。

通过 MCP/A2A 协议封装为标准化 AI Skills 模块，
构建低代码 Skill 框架与企业级可复用技能生态。

设计：
- 每个 Skill 对应一个 MCP Server，暴露标准化 tools/resources/prompts
- SkillRegistry 作为注册中心，Agent 运行时通过它发现并调用 Skill
- 新系统接入只需实现一个 Skill 类，体现"低代码、可复用"理念
"""
from __future__ import annotations

from .registry import SkillRegistry, Skill

__all__ = ["SkillRegistry", "Skill"]
