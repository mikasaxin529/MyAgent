"""DevPilot 全局配置。

从环境变量 / .env 加载，集中管理模型网关与各模块可调参数。
后续模块（RAG/MCP/Eval）的配置也统一挂在这里，避免散落。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


@dataclass
class ProviderConfig:
    """单个模型 Provider 的配置。"""
    name: str
    api_key: str
    base_url: str
    model: str

    @property
    def available(self) -> bool:
        return bool(self.api_key)


@dataclass
class GatewayConfig:
    primary: str = field(default_factory=lambda: _env("DEVILOT_PRIMARY_MODEL", "deepseek"))
    fallback: str = field(default_factory=lambda: _env("DEVILOT_FALLBACK_MODEL", "qwen"))
    rpm_limit: int = field(default_factory=lambda: int(_env("DEVILOT_RPM_LIMIT", "60") or "60"))


@dataclass
class Settings:
    gateway: GatewayConfig = field(default_factory=GatewayConfig)

    def providers(self) -> dict[str, ProviderConfig]:
        """返回所有已配置的 provider，按 name 索引。"""
        return {
            "deepseek": ProviderConfig(
                "deepseek",
                _env("DEEPSEEK_API_KEY"),
                _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                _env("DEEPSEEK_MODEL", "deepseek-chat"),
            ),
            "qwen": ProviderConfig(
                "qwen",
                _env("QWEN_API_KEY"),
                _env("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
                _env("QWEN_MODEL", "qwen-plus"),
            ),
            "openai": ProviderConfig(
                "openai",
                _env("OPENAI_API_KEY"),
                _env("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                _env("OPENAI_MODEL", "gpt-4o-mini"),
            ),
            # 本地 ollama：走 OpenAI 兼容端点（/v1），无需真实 API Key，
            # 给占位 "ollama" 满足 openai SDK 非空要求 + ProviderConfig.available 检查。
            "ollama": ProviderConfig(
                "ollama",
                _env("OLLAMA_API_KEY", "ollama"),
                _env("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
                _env("OLLAMA_MODEL", "qwen2.5:7b"),
            ),
        }


# ----------------------------------------------------------------------
# Agent → Model 绑定（加载 config/agents.yaml）
# ----------------------------------------------------------------------
# 体现"不同任务/agent 绑不同模型"：每个 langgraph 节点按 key 从此取
# (provider_name, model_name)。yaml 缺失或某 agent 未配置 → 回退到默认链。
# 每次 build graph 时调用（无缓存），故改 yaml 即时生效，无需重启。

# 默认绑定：所有 agent 都用 deepseek:deepseek-chat（最通用、最易跑通）。
# 用户编辑 config/agents.yaml 可覆盖，例如把 coder 换成 deepseek-coder。
_DEFAULT_AGENT_MODELS: dict[str, str] = {
    "router": "deepseek:deepseek-chat",
    "classifier": "deepseek:deepseek-chat",
    "chat": "deepseek:deepseek-chat",
    "websearch": "deepseek:deepseek-chat",
    "planner": "deepseek:deepseek-chat",
    "coder": "deepseek:deepseek-chat",
    "reviewer": "deepseek:deepseek-chat",
    "tester": "deepseek:deepseek-chat",
    # ChatFlow 式 SSE 图新节点（build_chat_graph 用）。
    "reflector": "deepseek:deepseek-chat",
    "extractor": "deepseek:deepseek-chat",
}


def _agents_yaml_path() -> str:
    """定位 config/agents.yaml：本文件在 src/devpilot/config.py，
    项目根 = parents[2]（devpilot → src → 项目根）。"""
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))  # 项目根
    return os.path.join(root, "config", "agents.yaml")


def load_agent_models() -> dict[str, tuple[str, str]]:
    """加载 agent→(provider_name, model_name) 映射。

    读取 config/agents.yaml 覆盖默认值。yaml 解析失败或文件缺失时
    优雅降级到 _DEFAULT_AGENT_MODELS，绝不抛错（保证 graph 可用）。

    返回：{agent_name: (provider_name, model_name)}，model_name 为空串表示
          用该 provider 的默认模型（见 ProviderConfig.model）。
    """
    merged: dict[str, str] = dict(_DEFAULT_AGENT_MODELS)
    path = _agents_yaml_path()
    try:
        import os
        if not os.path.exists(path):
            return _parse_provider_model(merged)
        import yaml  # 惰性 import：pyyaml 缺失时降级到默认
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(k, str) and isinstance(v, str):
                    merged[k] = v
    except Exception:  # noqa: BLE001 - 配置加载失败不该阻断，降级默认
        pass
    return _parse_provider_model(merged)


def _parse_provider_model(raw: dict[str, str]) -> dict[str, tuple[str, str]]:
    """把 "provider:model" 字符串解析成 (provider, model) 元组。

    兼容只写 provider（取其默认模型）和只写 model（provider 为空），
    但 yaml 约定用 "provider:model" 格式。
    """
    result: dict[str, tuple[str, str]] = {}
    for agent, val in raw.items():
        if ":" in val:
            provider, model = val.split(":", 1)
            result[agent] = (provider.strip(), model.strip())
        else:
            # 无冒号：当作 provider 名，model 留空用 provider 默认
            result[agent] = (val.strip(), "")
    return result


settings = Settings()
