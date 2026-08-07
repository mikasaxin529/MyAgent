"""langgraph 智能编排层：agent 路由 + 多 agent 分支 + 流式产出。

本模块用 langgraph 的 StateGraph 重写编排流（替换原 orchestrator 的顺序
硬编码 plan→coder→review→test），核心能力：

1. agent 路由（router 节点）：LLM 根据用户输入判断意图，分发到
   chat / websearch / dev 三条分支之一——"模型根据用户输入判断应该调用
   什么 agent"。
2. 模型绑定：每个节点按 config/agents.yaml 绑定自己的模型
   （router/chat/websearch/planner/coder/reviewer/tester 各一）——
   "不同任务支持不同模型，不同 agent 绑定指定模型"。
3. 流式产出：节点内调 gateway.stream_chat，逐 token yield ChatChunk，
   经 web 层转成 WS 帧推给前端——ChatGPT 式流式 + 思考过程。
4. 可观测：每个节点进出经 emitter 推 {type:"node", node_id, status}，
   前端右侧 @xyflow/react 图形流据此高亮节点——Dify 式图形流输出。

设计选型（为什么用 langgraph 而非继续手写编排）：
- StateGraph 的"状态 + 条件边"是声明式编排，新增 agent 分支只加节点+条件
  映射，不改既有节点；比 orchestrator 的顺序语句更易扩展。
- 与手写 ReAct（runtime/react.py）的分工：langgraph 管"多 agent 编排"，
  react.py 保留为"单 agent ReAct 循环"的教学对照实现——先手写理解机制，
  再用框架重构，两者互补不互斥。
"""
from __future__ import annotations

from .state import AgentGraphState
from .graph import build_graph, build_dynamic_graph, build_chat_graph_runtime

__all__ = ["AgentGraphState", "build_graph", "build_dynamic_graph", "build_chat_graph_runtime"]
