"""基于 OpenAI 兼容协议的 Provider 实现。

DeepSeek / 通义千问 / OpenAI 官方都兼容 OpenAI Chat Completions 接口，
因此一个实现即可覆盖多家。本地 vLLM 部署后同样指向 base_url 即可接入。
"""
from __future__ import annotations

import time
from typing import AsyncIterator

from openai import AsyncOpenAI, OpenAI

from .base import ChatChunk, ChatMessage, ChatResponse
from ..config import ProviderConfig


class OpenAICompatProvider:
    """OpenAI 兼容协议的通用 Provider。

    通过 ProviderConfig 构造，可适配 DeepSeek/Qwen/OpenAI/vLLM 等。
    同时提供同步 chat()（CLI/eval）与异步 stream_chat()（流式聊天）。
    """

    def __init__(self, cfg: ProviderConfig) -> None:
        self.name = cfg.name
        self._model = cfg.model
        self._client: OpenAI | None = None
        self._async_client: AsyncOpenAI | None = None
        self._cfg = cfg
        if cfg.available:
            # 懒初始化 client，避免 import 时即报错。
            # 同步与异步客户端各一份：chat() 用同步，stream_chat() 用异步。
            self._client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url)
            self._async_client = AsyncOpenAI(api_key=cfg.api_key, base_url=cfg.base_url)

    @property
    def available(self) -> bool:
        return self._client is not None

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        json_mode: bool = False,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> ChatResponse:
        if self._client is None:
            raise RuntimeError(f"provider {self.name} 未配置 API Key，无法调用")

        payload_messages = [m.to_dict() if hasattr(m, "to_dict") else m for m in messages]
        kwargs: dict = {
            "model": self._model,
            "messages": payload_messages,
            "temperature": temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if tools:
            kwargs["tools"] = tools
            if tool_choice is not None:
                kwargs["tool_choice"] = tool_choice

        t0 = time.perf_counter()
        resp = self._client.chat.completions.create(**kwargs)
        latency = int((time.perf_counter() - t0) * 1000)

        choice = resp.choices[0]
        usage = resp.usage
        msg = choice.message
        # 原生 function-calling：解析 assistant 发起的 tool_calls（OpenAI 格式），
        # 供上层图条件边判断是否流转给 ToolNode。finish_reason=="tool_calls" 即触发。
        raw_tcs = getattr(msg, "tool_calls", None)
        tool_calls = [tc.model_dump() for tc in raw_tcs] if raw_tcs else []
        fr = choice.finish_reason or ""
        return ChatResponse(
            content=msg.content or "",
            provider=self.name,
            model=self._model,
            latency_ms=latency,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            tool_calls=tool_calls,
            finish_reason=fr,
            metadata={"finish_reason": fr},
        )

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str = "",
        temperature: float = 0.7,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> AsyncIterator[ChatChunk]:
        """流式对话：用 AsyncOpenAI + stream=True，逐 chunk yield ChatChunk。

        关键：
        - DeepSeek-R1 等推理模型的"思考过程"在 chunk.delta.reasoning_content
          字段（OpenAI 标准协议无此字段，DeepSeek 扩展），作为 reasoning 透传。
        - 原生 function-calling：tools 非空时透传给 create()；OpenAI 流式协议
          把同一 index 的 tool_calls 分多片下发——function.name 仅首片携带，
          function.arguments 逐片拼接。这里每片只透传原始增量（tool_call_delta），
          聚合由 call_model 节点内完成（节点维护 acc dict[index]）。前端据
          tool_call_delta 逐字渲染 tool_call_args 帧。
        - finish_reason 在流末 chunk.choices[0].finish_reason，仅 done 帧携带。
        - model 参数：允许网关按"agent→model 绑定"传入具体模型名（覆盖 provider
          默认），体现"不同任务/agent 绑不同模型"。
        """
        if self._async_client is None:
            raise RuntimeError(f"provider {self.name} 未配置 API Key，无法流式调用")

        payload_messages = [m.to_dict() if hasattr(m, "to_dict") else m for m in messages]
        use_model = model or self._model
        create_kwargs: dict = {
            "model": use_model,
            "messages": payload_messages,
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            create_kwargs["tools"] = tools
            if tool_choice is not None:
                create_kwargs["tool_choice"] = tool_choice
        stream = await self._async_client.chat.completions.create(**create_kwargs)
        sent_meta = False
        finish_reason = ""
        async for chunk in stream:
            try:
                choice0 = chunk.choices[0]
                delta_obj = choice0.delta
                # 流末的终止原因（"stop"/"tool_calls"/"length"），仅最后一帧携带。
                fr = getattr(choice0, "finish_reason", None)
                if fr:
                    finish_reason = fr
            except (IndexError, AttributeError):
                # 个别 chunk 可能无 choices（如 OpenAI 的 usage 帧或 keepalive），跳过。
                continue
            delta_text = getattr(delta_obj, "content", "") or ""
            reasoning_text = getattr(delta_obj, "reasoning_content", "") or ""
            # 工具调用增量：OpenAI 流式把 tool_calls 放在 delta.tool_calls 列表，
            # 每个 element 带 index（区分并发调用）+ 首片的 id/function.name +
            # 逐片的 function.arguments 片段。取首元素透传（多数场景单工具调用）。
            tool_call_delta: dict | None = None
            raw_tcs = getattr(delta_obj, "tool_calls", None)
            if raw_tcs:
                tc = raw_tcs[0]
                fn = getattr(tc, "function", None)
                tool_call_delta = {
                    "index": getattr(tc, "index", 0) or 0,
                    "id": getattr(tc, "id", "") or "",
                    "function": {
                        "name": (getattr(fn, "name", "") or "") if fn else "",
                        "arguments": (getattr(fn, "arguments", "") or "") if fn else "",
                    },
                }
            has_payload = bool(delta_text or reasoning_text or tool_call_delta)
            if not has_payload and not sent_meta:
                # 仍先发一个首帧带来源信息，避免前端迟迟等不到响应。
                yield ChatChunk(provider=self.name, model=use_model)
                sent_meta = True
                continue
            if not sent_meta:
                # 首个有效内容帧附 provider/model（前端据此显示来源 meta）。
                yield ChatChunk(
                    delta=delta_text,
                    reasoning=reasoning_text,
                    tool_call_delta=tool_call_delta,
                    provider=self.name,
                    model=use_model,
                )
                sent_meta = True
            else:
                if has_payload:
                    yield ChatChunk(
                        delta=delta_text,
                        reasoning=reasoning_text,
                        tool_call_delta=tool_call_delta,
                    )
        # 流结束：发 done 帧让前端收尾，并携带 finish_reason 供图条件边判断。
        yield ChatChunk(done=True, provider=self.name, model=use_model, finish_reason=finish_reason)
