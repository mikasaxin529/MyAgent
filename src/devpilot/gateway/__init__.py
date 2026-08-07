"""模型网关：统一封装多模型调用，提供路由 / 限流 / 缓存 / fallback。

参与大模型工程化部署与推理优化，保障稳定性、响应效率与安全合规。

设计要点：
- Provider 抽象：DeepSeek/Qwen/OpenAI 都走 OpenAI 兼容协议，统一适配
- 网关职责：按配置路由主模型，失败自动 fallback，RPM 限流，简易缓存
- 对外只暴露 Gateway.chat()，上层 Agent 运行时不感知具体模型
"""
from __future__ import annotations

from .base import LLMProvider, ChatMessage, ChatResponse, ChatChunk
from .providers import OpenAICompatProvider
from .gateway import Gateway, build_default_gateway

__all__ = [
    "LLMProvider",
    "ChatMessage",
    "ChatResponse",
    "ChatChunk",
    "OpenAICompatProvider",
    "Gateway",
    "build_default_gateway",
]
