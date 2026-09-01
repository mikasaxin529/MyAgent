"""模型网关核心：路由 / fallback / 限流 / 缓存。

网关是 Agent 与具体模型之间唯一的边界，职责：
1. 路由：按配置选主模型
2. fallback：主模型失败自动切备模型，保障稳定性
3. 限流：RPM 上限，防止突发流量打爆配额
4. 缓存：相同请求简易缓存，降低成本与延迟（生产建议接 Redis）

后续私有化模型（vLLM）也只需注册成 Provider 即可纳入网关统一调度。
"""
from __future__ import annotations

import time
from collections import deque
from typing import AsyncIterator, Callable

from .base import ChatChunk, ChatMessage, ChatResponse, LLMProvider
from ..config import settings


class Gateway:
    """模型网关。对外暴露 chat()，内部负责路由与容错。"""

    def __init__(self) -> None:
        self._providers: dict[str, LLMProvider] = {}
        self._primary: str = settings.gateway.primary
        self._fallback: str = settings.gateway.fallback
        self._rpm_limit: int = settings.gateway.rpm_limit
        # 滑动窗口限流：记录最近一分钟内的调用时间戳
        self._call_timestamps: deque[float] = deque()
        # 简易内存缓存：hash(消息+参数) -> response。生产换 Redis。
        self._cache: dict[str, ChatResponse] = {}

    # ---- 注册 ----
    def register(self, provider: LLMProvider) -> None:
        self._providers[provider.name] = provider

    @property
    def available_providers(self) -> list[str]:
        return [n for n, p in self._providers.items() if p.available]

    # ---- 路由 ----
    def _pick_chain(self) -> list[str]:
        """返回 [主模型, 备模型] 中可用的有序链。"""
        chain: list[str] = []
        for name in [self._primary, self._fallback]:
            if name and name not in chain:
                chain.append(name)
        return [n for n in chain if n in self._providers and self._providers[n].available]

    # ---- 限流 ----
    def _acquire_rpm(self) -> None:
        now = time.time()
        while self._call_timestamps and now - self._call_timestamps[0] > 60:
            self._call_timestamps.popleft()
        if len(self._call_timestamps) >= self._rpm_limit:
            wait = 60 - (now - self._call_timestamps[0])
            raise RuntimeError(f"触发 RPM 限流({self._rpm_limit}/min)，请 {wait:.1f}s 后重试")
        self._call_timestamps.append(now)

    # ---- 对外接口 ----
    def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        json_mode: bool = False,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        use_cache: bool = False,
    ) -> ChatResponse:
        """统一的对话入口。

        失败时沿 fallback 链逐个重试，全部失败才抛错。
        任何一次成功即返回（fail-fast on provider errors, fail-over to next）。
        """
        key = ""
        if use_cache:
            key = self._cache_key(messages, temperature, json_mode)
            if key in self._cache:
                return self._cache[key]

        self._acquire_rpm()

        errors: list[str] = []
        for name in self._pick_chain():
            try:
                resp = self._providers[name].chat(
                    messages, temperature=temperature, json_mode=json_mode,
                    tools=tools, tool_choice=tool_choice,
                )
                if use_cache and key:
                    self._cache[key] = resp
                return resp
            except Exception as e:  # noqa: BLE001 - 网关要捕获所有 provider 错误以触发 fallback
                errors.append(f"{name}: {e!r}")
                continue

        raise RuntimeError("所有模型均不可用，错误明细：\n" + "\n".join(errors))

    def chat_text(self, prompt: str, *, system: str = "", **kw) -> str:
        """便捷方法：单轮文本 prompt 直接拿回字符串。"""
        msgs: list[ChatMessage] = []
        if system:
            msgs.append(ChatMessage("system", system))
        msgs.append(ChatMessage("user", prompt))
        return self.chat(msgs, **kw).content

    # ------------------------------------------------------------------
    # 流式对话（ChatGPT 式逐 token 输出）
    # ------------------------------------------------------------------
    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        provider: str = "",
        model: str = "",
        temperature: float = 0.7,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> AsyncIterator[ChatChunk]:
        """流式对话入口：按 provider 路由，逐 chunk yield ChatChunk。

        设计要点（与同步 chat 的差异）：
        - 按"agent→model 绑定"传入 provider 名（来自 config/agents.yaml），
          体现"不同任务/agent 绑不同模型"；provider 为空则走默认主备链。
        - fail-over：主 provider 流式过程中首帧前失败则切备；一旦开始产出
          内容（已 yield）就不再切换（中途切换会导致内容断裂，不如报错由
          上层重试）——流式的容错策略与同步不同，同步可整体重试，流式
          一旦开流就"覆水难收"。
        - 限流：流式同样走 _acquire_rpm，与同步共用配额。
        - 缓存：流式不缓存（流式语义是实时增量，缓存无意义）。

        Args:
            messages: 对话消息列表。
            provider: 指定 provider 名（来自 agents.yaml 绑定）。空走默认链。
            model: 指定模型名（覆盖 provider 默认模型）。
            temperature: 采样温度。
        Yields:
            ChatChunk：含 delta（正文增量）/ reasoning（思考增量）/ done。
        """
        self._acquire_rpm()

        # provider 为空 → 走默认主备链（_pick_chain 返回 [主, 备]）。
        chain = [provider] if provider else self._pick_chain()
        # 过滤掉不在 _providers 或不可用的 provider。
        chain = [n for n in chain if n in self._providers and self._providers[n].available]
        if not chain:
            raise RuntimeError(f"无可用的 provider（请求 provider={provider or '默认链'}）")

        errors: list[str] = []
        started = False  # 是否已开始产出内容（已 yield 则不再 fail-over）
        for name in chain:
            try:
                async for chunk in self._providers[name].stream_chat(
                    messages, model=model, temperature=temperature,
                    tools=tools, tool_choice=tool_choice,
                ):
                    started = True
                    yield chunk
                return  # 正常流完，结束。
            except Exception as e:  # noqa: BLE001 - 流式也需 fail-over
                errors.append(f"{name}: {e!r}")
                if started:
                    # 已开始产出后失败：不再切换，向上抛出（避免内容断裂）。
                    raise RuntimeError(f"流式中断（{name}）：{e!r}") from e
                # 首帧前失败：切下一个 provider 继续尝试。
                continue

        raise RuntimeError("所有 provider 流式均不可用：\n" + "\n".join(errors))

    def provider_for(self, provider: str) -> LLMProvider | None:
        """按名取 provider，用于 graph 节点构造独立 ChatModel。
        不可用返回 None（上层据 yaml 绑定降级到默认链）。"""
        if provider and provider in self._providers and self._providers[provider].available:
            return self._providers[provider]
        return None

    def _cache_key(self, messages: list[ChatMessage], temperature: float, json_mode: bool) -> str:
        raw = "|".join(m.to_dict()["content"] for m in messages)
        return f"{raw}|{temperature}|{json_mode}"


def build_default_gateway() -> Gateway:
    """按 settings 注册所有已配置 provider，返回可用网关。

    若没有任何 provider 配了 API Key，会抛出友好提示。
    """
    from .providers import OpenAICompatProvider

    gw = Gateway()
    for name, cfg in settings.providers().items():
        gw.register(OpenAICompatProvider(cfg))  # type: ignore[arg-type]

    if not gw.available_providers:
        raise RuntimeError(
            "未检测到任何已配置 API Key 的模型，请在 .env 中至少配置一个 "
            "(DEEPSEEK_API_KEY / QWEN_API_KEY / OPENAI_API_KEY)"
        )
    return gw
