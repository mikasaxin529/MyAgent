"""langgraph 图状态定义：节点间共享的编排状态。

AgentGraphState 是 langgraph StateGraph 的状态类型（TypedDict），
所有节点读写它，实现"黑板模式"的解耦——节点之间不直接调用，
通过 state 字段通信（如 router 写 route，条件边读 route 决定下一节点）。

字段对应三类分支的中间产物：
- 通用：task（原始输入）、messages（对话历史，langgraph 惯例）、route（路由结果）、
  final_answer（最终输出）、nodes_visited（图形流可视化用，节点进出记录）。
- chat 分支：无需额外字段，直接写 final_answer。
- websearch 分支：search_results（搜索结果）、search_query（搜索词）。
- dev 分支：plan / code_diff / review / test_result / approval（沿用原黑板语义）。
"""
from __future__ import annotations

from typing import TypedDict


class AgentGraphState(TypedDict, total=False):
    """langgraph 编排状态。total=False：所有字段可选，节点只更新自己负责的子集。

    langgraph 的 StateGraph 会在节点返回的 dict 上做"增量合并"——节点返回
    {字段: 新值}，框架合并进全局 state。total=False 让每个节点可只返回自己改的字段。
    """
    # 通用
    task: str
    messages: list  # 对话历史（list[dict]，OpenAI 消息格式）
    intent: str  # classifier 判定：simple | complex（动态图意图分流）
    route: str  # router 判定：chat | websearch | dev
    route_reason: str  # 路由理由（前端展示"模型为何选这条分支"）
    final_answer: str  # 最终答案文本
    nodes_visited: list  # 已访问节点 id 列表（图形流高亮用）

    # websearch 分支
    search_query: str
    search_results: str

    # dev 分支（沿用原 Blackboard 语义）
    plan: list
    code_diff: str
    review: str
    test_result: str
    approval: dict  # 审批裁决结果（高危动作时填充）

    # 动态编排（plan-and-execute）：planner 生成步骤计划，executor 循环执行
    plan_steps: list  # [{id, name, description, output, needs_search}, ...]
    step_index: int  # 当前执行到第几步
    step_results: list  # 每步产出文本，供下一步作上下文

    # ChatFlow 式编排（route→planner→call_model⇄tools→after_tool→reflector→save→memory）
    # 与上面 plan-and-execute 并存：新 SSE 图（build_chat_graph）用这组字段。
    user_message: str  # 原始用户输入（与 task 同义，对齐 ChatFlow 命名）
    tool_model: str  # 路由后选定的工具调用模型（call_model 用）
    answer_model: str  # 路由后选定的综合回答模型（call_model_after_tool 用）
    plan: list  # ChatFlow 式步骤 [{id, title, description, status, result}, ...]
    current_step_index: int  # 当前执行到第几步（新图）
    step_iterations: int  # 当前步的循环次数（reflector retry 计数）
    full_response: str  # call_model 累积的回复正文（reflector/save_response 读）
    reflector_decision: str  # reflector 判定：done | continue | retry
    reflection: str  # reflector 的反思文本（前端展示）
    tool_messages: list  # 工具返回消息累积（should_continue_after_tool 统计用）
    session_id: str  # 会话 id（SSE 端点传入，memory 节点落库用）
