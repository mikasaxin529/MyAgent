"""langgraph 图组装：把节点连成带条件边的 StateGraph。

图结构：
    START → router ─(条件边按 route)→ chat     → END
                       │                → websearch → END
                       └─dev→ planner → coder → reviewer → tester → END

条件边（router 出边）：读 state["route"] 决定下一节点。
- chat/websearch 分支是"单节点→END"，轻量直接。
- dev 分支是固定顺序链 planner→coder→reviewer→tester→END（研发闭环）。

节点间通过 state 字段通信（黑板模式）：planner 写 plan，coder 读 plan 写
code_diff，reviewer 读 code_diff 写 review，tester 读 code_diff 调 cicd。
"""
from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from .state import AgentGraphState
from .nodes import (
    make_chat_node,
    make_coder_node,
    make_planner_node,
    make_reviewer_node,
    make_router_node,
    make_tester_node,
    make_websearch_node,
    NODE_ROUTER, NODE_CHAT, NODE_WEBSEARCH, NODE_PLANNER, NODE_CODER, NODE_REVIEWER, NODE_TESTER,
)
from .dynamic_nodes import (
    make_planner_node as make_dyn_planner,
    make_executor_node,
    make_classifier_node,
    make_direct_chat_node,
    should_continue,
    _route_intent,
)
from .cf import (
    make_route_node,
    make_planner_node as make_cf_planner,
    make_call_model_node,
    make_call_model_after_tool_node,
    make_tool_node,
    make_reflector_node,
    make_save_response_node,
    make_extract_memory_node,
    make_compress_memory_node,
)


def _route_fn(state: AgentGraphState) -> str:
    """条件边路由函数：读 route 决定 router 之后去哪个节点。

    route 未设置（router 失败）时降级到 chat（最安全）。
    """
    route = state.get("route", "chat")
    if route == "websearch":
        return NODE_WEBSEARCH
    if route == "dev":
        return NODE_PLANNER
    return NODE_CHAT  # chat 或未知


def build_graph(gateway, registry, audit=None, approval=None, emitter=None) -> Any:
    """组装并编译 langgraph 图，返回 CompiledGraph。

    参数：
        gateway:  模型网关（提供 stream_chat / chat）。
        registry: Skill 注册中心（websearch/cicd 等 skill 来源）。
        audit:    审计日志（可选），dev 分支各节点记审计用。
        approval: 审批门（可选），reviewer 高危时走人工审批。
        emitter:  事件回调 Callable[[dict], None]，节点把 token/reasoning/
                  node/route/blackboard/step 帧推给它，web 层转成 WS 帧。

    返回：langgraph 编译后的图，可 .astream(input) 异步流式产出状态更新。
    """
    graph = StateGraph(AgentGraphState)

    # 注册所有节点（用工厂函数构造，闭包捕获依赖）。
    graph.add_node(NODE_ROUTER, make_router_node(gateway, emitter))
    graph.add_node(NODE_CHAT, make_chat_node(gateway, emitter))
    graph.add_node(NODE_WEBSEARCH, make_websearch_node(gateway, registry, emitter))
    graph.add_node(NODE_PLANNER, make_planner_node(gateway, audit, emitter))
    graph.add_node(NODE_CODER, make_coder_node(gateway, registry, audit, emitter))
    graph.add_node(NODE_REVIEWER, make_reviewer_node(gateway, audit, approval, emitter))
    graph.add_node(NODE_TESTER, make_tester_node(gateway, registry, audit, emitter))

    # 入口：router。
    graph.set_entry_point(NODE_ROUTER)

    # router 条件出边：按 route 分发到 chat/websearch/planner。
    graph.add_conditional_edges(
        NODE_ROUTER,
        _route_fn,
        {
            NODE_CHAT: NODE_CHAT,
            NODE_WEBSEARCH: NODE_WEBSEARCH,
            NODE_PLANNER: NODE_PLANNER,
        },
    )

    # chat / websearch 分支：单节点直接到 END。
    graph.add_edge(NODE_CHAT, END)
    graph.add_edge(NODE_WEBSEARCH, END)

    # dev 分支：planner → coder → reviewer → tester → END（研发闭环）。
    graph.add_edge(NODE_PLANNER, NODE_CODER)
    graph.add_edge(NODE_CODER, NODE_REVIEWER)
    graph.add_edge(NODE_REVIEWER, NODE_TESTER)
    graph.add_edge(NODE_TESTER, END)

    return graph.compile()


def build_chat_graph_runtime(audit=None, approval=None):
    """装配 chat graph 运行时：返回 (gateway, registry, audit, approval, graph, emitter_setter)。

    与 web.runtime.build_runtime 平行的工厂，供 /ws/chat 使用。
    emitter 默认 None，调用方通过返回的 emitter 设置器注入帧回调。

    返回：
        (gateway, registry, audit, approval, graph, set_emitter)
        graph 节点已编译；set_emitter(fn) 可在运行时切换 emitter（每次请求重置）。
    """
    from ..gateway import build_default_gateway
    from ..web.runtime import build_registry
    from ..governance.audit import AuditLog
    from ..governance.approval import ApprovalGate

    gw = build_default_gateway()
    registry = build_registry()
    if audit is None:
        audit = AuditLog()
    if approval is None:
        approval = ApprovalGate()

    # emitter 用一个可变容器持有，便于每次请求替换。
    holder: dict = {"emitter": None}

    def set_emitter(fn) -> None:
        holder["emitter"] = fn

    # graph 在装配时绑定 holder["emitter"]（节点通过闭包读最新值）。
    # 动态图不接 approval（Chat 路径不走人工审批，高危动作直接拒绝由 ApprovalGate 默认行为兜底）。
    graph = build_chat_graph(gw, registry, audit=audit,
                             emitter=lambda f: _emit_via_holder(holder, f))

    return gw, registry, audit, approval, graph, set_emitter


def _emit_via_holder(holder: dict, frame: dict) -> None:
    """从 holder 取当前 emitter 推帧。

    节点闭包里直接调 holder["emitter"](f) 也能工作，但包一层便于加日志/容错。
    """
    fn = holder.get("emitter")
    if fn is None:
        return
    try:
        fn(frame)
    except Exception:  # noqa: BLE001
        pass


# ----------------------------------------------------------------------
# 动态编排图（plan-and-execute）—— Chat 页用
# ----------------------------------------------------------------------
def build_dynamic_graph(gateway, registry, audit=None, emitter=None) -> Any:
    """组装动态编排图：classifier → (simple: direct | complex: planner→executor) → END。

    意图分流架构：
    - classifier（ollama）快速判 simple/complex。
      - simple → direct_chat（ollama 流式直答，不规划）→ END。
      - complex → planner（deepseek）动态生成步骤 → executor 循环执行 → END。
    - 任何 classifier 失败兜底 complex（走 deepseek 规划最稳）。

    与 build_graph（固定分支）的区别：planner 据 input 动态生成 N 步计划，
    executor 循环执行；节点数/名随任务变。前端据 plan 帧动态画图。
    简单对话分支不产 plan 帧，前端不画步骤图。
    """
    graph = StateGraph(AgentGraphState)
    graph.add_node("classifier", make_classifier_node(gateway, emitter))
    graph.add_node("direct", make_direct_chat_node(gateway, emitter))
    graph.add_node("planner", make_dyn_planner(gateway, registry, emitter))
    graph.add_node("executor", make_executor_node(gateway, registry, emitter))

    # 入口：classifier 意图分流。
    graph.set_entry_point("classifier")
    # classifier 条件出边：按 intent 分发到 direct（简单）或 planner（复杂）。
    graph.add_conditional_edges(
        "classifier",
        _route_intent,
        {"direct": "direct", "planner": "planner"},
    )
    # simple 分支：direct 直答后到 END。
    graph.add_edge("direct", END)
    # complex 分支：planner → executor 开始第一步。
    graph.add_edge("planner", "executor")
    # executor 后条件边：还有步骤 → executor（循环）；否则 → END。
    graph.add_conditional_edges(
        "executor",
        should_continue,
        {"executor": "executor", "__end__": END},
    )

    return graph.compile()


# ----------------------------------------------------------------------
# ChatFlow 式 SSE 图（route→planner→call_model⇄tools→after_tool→reflector→save→memory）
# —— 新 /api/chat SSE 端点用本图。原生 function-calling + reflector 反思循环 +
#    memory JSONL（无 DB）。排除沙箱/语义缓存/RAG。与 build_dynamic_graph 并存。
# ----------------------------------------------------------------------
def build_chat_graph(gateway, registry, audit=None, emitter=None) -> Any:
    """组装 ChatFlow 式 SSE 图。

    节点用 emitter 回调推 ChatFlow 帧（thinking/content/route/plan/tool_call/
    tool_result/search_item/reflection/status/memory），SSE 端点 drain 队列序列化
    为 data:{json}\\n\\n。原生 function-calling：call_model 产 tool_calls，
    ToolNode 执行，call_model_after_tool 综合，reflector 判 done/continue/retry。
    """
    from .edges import (
        reflector_routing,
        should_continue_after_tool,
        should_continue as cf_should_continue,
    )

    graph = StateGraph(AgentGraphState)
    graph.add_node("route_model", make_route_node(gateway, registry, emitter))
    graph.add_node("planner", make_cf_planner(gateway, registry, emitter))
    graph.add_node("call_model", make_call_model_node(gateway, registry, emitter))
    graph.add_node("tools", make_tool_node(gateway, registry, emitter))
    graph.add_node("call_model_after_tool",
                   make_call_model_after_tool_node(gateway, registry, emitter))
    graph.add_node("reflector", make_reflector_node(gateway, registry, emitter))
    graph.add_node("save_response", make_save_response_node(gateway, audit, emitter))
    graph.add_node("extract_memory", make_extract_memory_node(gateway, audit, emitter))
    graph.add_node("compress_memory", make_compress_memory_node(gateway, audit, emitter))

    graph.set_entry_point("route_model")
    graph.add_edge("route_model", "planner")
    graph.add_edge("planner", "call_model")
    graph.add_conditional_edges(
        "call_model", cf_should_continue,
        {"tools": "tools", "reflector": "reflector", "save_response": "save_response"},
    )
    graph.add_edge("tools", "call_model_after_tool")
    graph.add_conditional_edges(
        "call_model_after_tool", should_continue_after_tool,
        {"tools": "tools", "reflector": "reflector", "save_response": "save_response"},
    )
    graph.add_conditional_edges(
        "reflector", reflector_routing,
        {"call_model": "call_model", "save_response": "save_response"},
    )
    graph.add_edge("save_response", "extract_memory")
    graph.add_edge("extract_memory", "compress_memory")
    graph.add_edge("compress_memory", END)
    return graph.compile()
