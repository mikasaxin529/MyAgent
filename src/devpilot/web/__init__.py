"""DevPilot Web 层：HTTP API + WebSocket，对接前端。

设计：本层只做"协议适配"——把现有 CLI 能力（gateway/skills/orchestrator/eval）
暴露为 REST + WS，不重复实现任何业务逻辑。CLI 与 Web 共用同一套装配工厂
（runtime.build_runtime），保证行为一致、单一事实源。
"""
from __future__ import annotations

__all__ = ["build_runtime"]
