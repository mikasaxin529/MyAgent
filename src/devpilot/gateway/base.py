"""模型网关的抽象类型定义。

把"一次对话调用"抽象成统一接口，底层无论是 DeepSeek、Qwen 还是本地 vLLM，
都实现 LLMProvider。这样上层 Agent 运行时只跟抽象打交道，模型可热切换。

原生 function-calling 支持（对齐 ChatFlow）：
- ChatMessage 携带 tool_calls / tool_call_id / name，使 assistant 工具调用
  消息与 role="tool" 的工具返回消息能在网关层往返。
- ChatChunk.tool_call_delta 携带流式 tool_calls 分片增量（OpenAI 流式协议
  把同一 index 的 function.arguments 分多片下发），供前端逐字渲染参数。
- ChatResponse.tool_calls / finish_reason 供图条件边判断是否进入 ToolNode。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol


@dataclass
class ChatMessage:
    """一条对话消息。role: system | user | assistant | tool。

    - tool_calls: assistant 消息携带的函数调用，OpenAI 格式
      [{id, type:"function", function:{name, arguments(JSON 字符串)}}]。
    - tool_call_id / name: role="tool" 的工具返回消息用，关联对应 tool_call。
    """
    role: str
    content: str
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    name: str = ""

    def to_dict(self) -> dict:
        """按角色序列化为 OpenAI 兼容 dict。

        - role="tool"：必须带 tool_call_id（关联触发它的 assistant tool_call）。
        - assistant 且 tool_calls 非空：带 tool_calls（content 可为空串，
          OpenAI 允许 assistant 仅发起工具调用而无文本）。
        - 其余：标准 {role, content}。
        """
        if self.role == "tool":
            return {
                "role": "tool",
                "content": self.content,
                "tool_call_id": self.tool_call_id or "",
                "name": self.name,
            }
        if self.role == "assistant" and self.tool_calls:
            return {
                "role": "assistant",
                "content": self.content or "",
                "tool_calls": self.tool_calls,
            }
        return {"role": self.role, "content": self.content}


@dataclass
class ChatResponse:
    """一次 LLM 调用的结构化返回（非流式）。

    metadata 记录 provider、耗时、token 用量等，供审计与评估使用。
    tool_calls / finish_reason 用于原生 function-calling：当 finish_reason
    == "tool_calls" 时，上层图条件边把消息流转给 ToolNode 执行。
    """
    content: str
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class ChatChunk:
    """流式调用产生的单个增量片段。

    流式输出（streaming）与 ChatGPT 式体验的关键：
    - delta: 本片段新增的正文文本（逐 token 拼接成最终回答）。
    - reasoning: 本片段新增的"思考过程"文本。DeepSeek-R1 等推理模型在
      chunk.delta.reasoning_content 里返回内部推理链，单独透传到前端
      折叠区展示——这是"体现思考过程"的来源。
    - tool_call_delta: 本片段新增的工具调用分片。OpenAI 流式协议把同一
      index 的 function.name（首片）+ function.arguments（逐片拼接）分多
      片下发；provider 只透传每片原始增量，聚合在 call_model 节点内完成
      （节点维护 acc dict[index] 合并）。前端据此逐字渲染 tool_call_args。
    - finish_reason: 流末的终止原因（"stop"/"tool_calls"/"length"...），
      仅 done 帧携带，供图条件边判断。
    - done: 是否是最后一帧（provider 流结束标志），前端据此收尾。
    - provider/model: 仅首帧携带，前端展示来源。
    """
    delta: str = ""
    reasoning: str = ""
    tool_call_delta: dict | None = None
    finish_reason: str = ""
    provider: str = ""
    model: str = ""
    done: bool = False


class LLMProvider(Protocol):
    """模型 Provider 协议：实现 chat / stream_chat 即可被网关调度。"""

    name: str

    def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        json_mode: bool = False,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> ChatResponse: ...

    def stream_chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str = "",
        temperature: float = 0.7,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> AsyncIterator[ChatChunk]: ...

    @property
    def available(self) -> bool: ...
