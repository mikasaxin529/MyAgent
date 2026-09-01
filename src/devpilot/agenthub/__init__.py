"""AgentHub 注册中心：目录扫描自动发现智能体。

每个子目录代表一个 agent，含 manifest.py 与 graph.py。
通用对话也注册为 agent（agent_id="general"）。

设计要点：
- 目录扫描式注册：新增智能体只需在 agenthub/ 下加一个包（manifest.py +
  graph.py），无需改动核心代码。
- 降级跳过：单个 agent 包 import 失败（manifest 或 graph 缺失/出错）时，
  记录 warning 日志并跳过该包，绝不拖垮整个服务。
- 模块级缓存：首次 import 时扫描一次，list_agents/get_agent 走缓存。
"""
from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_HUB = Path(__file__).resolve().parent


class AgentManifest:
    """智能体清单：注册中心对外的数据视图。"""

    def __init__(
        self,
        agent_id: str,
        display_name: str,
        description: str,
        identity_color: str,
        placeholder: str,
        managed_system: bool = False,
    ) -> None:
        self.agent_id = agent_id
        self.display_name = display_name
        self.description = description
        self.identity_color = identity_color
        self.placeholder = placeholder
        # 端点层是否替本智能体注入 SYSTEM_CHAT。
        # False（默认）= 图自管 system 消息（如 yuwen），端点不注入。
        self.managed_system = managed_system
        self.graph_fn: Callable[..., Any] | None = None

    def to_dict(self) -> dict:
        """转 REST 响应字段（/api/agents 用，id 为对外键）。"""
        return {
            "id": self.agent_id,
            "display_name": self.display_name,
            "description": self.description,
            "identity_color": self.identity_color,
            "placeholder": self.placeholder,
        }


def _discover() -> dict[str, AgentManifest]:
    """扫描 agenthub/ 子目录，发现所有 agent。

    返回 {agent_id: AgentManifest}。
    每个子目录必须含 manifest.py 与 graph.py；缺任一文件或 import 失败时，
    记 warning 并跳过该包（单个包失败不能拖垮整个注册中心）。
    """
    agents: dict[str, AgentManifest] = {}
    for entry in sorted(_HUB.iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        manifest_py = entry / "manifest.py"
        graph_py = entry / "graph.py"
        if not manifest_py.exists() or not graph_py.exists():
            logger.warning(
                "agenthub: 跳过 %s —— 缺少 manifest.py 或 graph.py", entry.name
            )
            continue
        try:
            mod = importlib.import_module(
                f"devpilot.agenthub.{entry.name}.manifest"
            )
        except Exception:  # noqa: BLE001 - 单个包失败降级跳过
            logger.warning("agenthub: manifest import 失败，跳过 %s", entry.name, exc_info=True)
            continue
        agent_id = getattr(mod, "AGENT_ID", entry.name)
        m = AgentManifest(
            agent_id=agent_id,
            display_name=getattr(mod, "DISPLAY_NAME", agent_id),
            description=getattr(mod, "DESCRIPTION", ""),
            identity_color=getattr(mod, "IDENTITY_COLOR", "#3D6CC4"),
            placeholder=getattr(mod, "PLACEHOLDER", ""),
            managed_system=getattr(mod, "MANAGED_SYSTEM", False),
        )
        try:
            graph_mod = importlib.import_module(
                f"devpilot.agenthub.{entry.name}.graph"
            )
            m.graph_fn = getattr(graph_mod, "build_graph", None)
        except Exception:  # noqa: BLE001 - graph 缺失降级（agent 仍可列出，执行时报错）
            logger.warning("agenthub: graph import 失败，%s 将不可执行", entry.name, exc_info=True)
            m.graph_fn = None
        agents[agent_id] = m
    return agents


# 模块级缓存，首次 import 时扫描一次。
_AGENTS: dict[str, AgentManifest] | None = None


def _ensure_loaded() -> dict[str, AgentManifest]:
    """惰性加载缓存（线程安全由 GIL + 幂等赋值保证）。"""
    global _AGENTS
    if _AGENTS is None:
        _AGENTS = _discover()
    return _AGENTS


def list_agents() -> list[AgentManifest]:
    """返回所有已发现的智能体清单。"""
    return list(_ensure_loaded().values())


def get_agent(agent_id: str) -> AgentManifest | None:
    """按 agent_id 查找智能体。"""
    return _ensure_loaded().get(agent_id)


def reset_cache() -> None:
    """清空模块级缓存（测试用），下次 list/get 重新扫描。"""
    global _AGENTS
    _AGENTS = None
