"""langgraph 节点实现：每个节点是一个 (state) -> state 局部更新 的函数。

节点职责与分工（对应"模型根据用户输入判断调用什么 agent"）：
- router_node：LLM 判意图，输出 route 标签——"agent 路由"中枢。
- chat_node：闲聊/问答，绑 chat 模型，流式直答。
- websearch_node：联网搜索，先调 WebSearchSkill 取结果，再绑 websearch 模型总结。
- dev 分支 planner/coder/reviewer/tester：复用原 agents.py 的研发闭环逻辑，
  Reviewer 高危仍调 approval.request 走人工审批（Human-on-the-Loop）。

每个节点：
1. 进/出时调 emitter 推 {type:"node", node_id, status:"running"|"done"}，驱动
   前端图形流高亮。
2. 流式节点（chat/websearch/planner/coder/review/tester）调 gateway.stream_chat，
   逐 chunk 经 emitter 推 {type:"token"|"reasoning", delta}，驱动前端 ChatGPT 式输出。
3. 节点只返回自己负责的 state 字段（langgraph 增量合并）。

模型绑定：每个节点从 load_agent_models()[节点名] 取 (provider, model)，
体现"不同 agent 绑不同模型"。
"""
from __future__ import annotations

import json
from typing import Any

from ..config import load_agent_models
from ..gateway import ChatMessage
from .state import AgentGraphState


# 节点 id 常量（前后端图形流共用，确保一致）。
NODE_ROUTER = "router"
NODE_CHAT = "chat"
NODE_WEBSEARCH = "websearch"
NODE_PLANNER = "planner"
NODE_CODER = "coder"
NODE_REVIEWER = "reviewer"
NODE_TESTER = "tester"

# 对话历史 system 文案（ws_chat 装配 messages 时首条 + chat_node 降级用）。
SYSTEM_CHAT = "你是 DevPilot 的助手，简洁专业地回答用户。"


def _history_messages(state) -> list:
    """从 state['messages'] 提取历史多轮（非 system），供节点拼 [自己的system]+历史+[当前增强user]。
    返回 ChatMessage 列表（含当前 prompt 那条 user；调用方按需 hist[:-1] 去掉当前 prompt）。"""
    msgs = state.get("messages") or []
    return [
        ChatMessage(m["role"], m["content"])
        for m in msgs
        if isinstance(m, dict) and m.get("content") and m.get("role") != "system"
    ]


def _state_messages_full(state, fallback_task: str) -> list:
    """全量多轮消息（含 system），供 chat_node 直接喂 gateway。降级 [system, user(task)]。"""
    msgs = state.get("messages") or []
    out = [
        ChatMessage(m["role"], m["content"])
        for m in msgs
        if isinstance(m, dict) and m.get("content")
    ]
    return out or [ChatMessage("system", SYSTEM_CHAT), ChatMessage("user", fallback_task)]


def _recent_history_text(state, n: int = 4) -> str:
    """最近 n 条非 system 消息拼 [role] content 文本，供 planner 附进 prompt 感知上文。
    排除最后一条（当前 prompt，避免与节点自身 user(task) 重复）。"""
    msgs = state.get("messages") or []
    hist = [
        m
        for m in msgs
        if isinstance(m, dict) and m.get("content") and m.get("role") != "system"
    ]
    hist = hist[:-1]  # 去掉当前 prompt
    return "\n".join(f"[{m['role']}] {m['content']}" for m in hist[-n:])


def _emit(emitter, frame: dict) -> None:
    """安全推送一帧给 web 层（emitter 可能为 None，CLI 路径）。"""
    if emitter is None:
        return
    try:
        emitter(frame)
    except Exception:  # noqa: BLE001 - 推送失败不影响主流程
        pass


def _visit(state: AgentGraphState, node_id: str, emitter) -> list:
    """记录节点访问（nodes_visited），并推送 node running 帧给图形流。

    返回更新后的 nodes_visited 列表（节点返回它让 langgraph 合并）。
    """
    visited = list(state.get("nodes_visited", []))
    if node_id not in visited:
        visited.append(node_id)
    _emit(emitter, {"type": "node", "node_id": node_id, "status": "running"})
    return visited


def _done(emitter, node_id: str) -> None:
    """节点完成：推 node done 帧给图形流（高亮变绿）。"""
    _emit(emitter, {"type": "node", "node_id": node_id, "status": "done"})


def _build_chat_model_kwargs(agent_key: str) -> dict:
    """从 agents.yaml 取该 agent 绑定的 provider/model，返回 stream_chat 入参。

    体现"不同 agent 绑不同模型"：节点按自己 key 取绑定。
    """
    models = load_agent_models()
    provider, model = models.get(agent_key, ("deepseek", "deepseek-chat"))
    return {"provider": provider, "model": model}


# ======================================================================
# Router 节点：agent 路由中枢
# ======================================================================
def make_router_node(gateway, emitter=None):
    """构造 router 节点：用 LLM 判意图，输出 route ∈ {chat, websearch, dev}。

    路由策略（为什么用 LLM 而非关键词规则）：
    - 规则路由需维护关键词表，泛化差、维护累；LLM 路由理解语义，一句"今天
      新闻"也能判为 websearch，一句"加个工具函数"也能判为 dev。
    - 用结构化输出（JSON）约束 LLM 只返回 {route, reason}，便于解析。
    - 降级：LLM 解析失败或不可用 → 默认 chat（最安全，至少能回话）。
    """

    async def router_node(state: AgentGraphState) -> dict:
        task = state.get("task", "")
        visited = _visit(state, NODE_ROUTER, emitter)

        models = load_agent_models()
        provider, model = models.get("router", ("deepseek", "deepseek-chat"))

        system = (
            "你是 DevPilot 的意图路由器。判断用户输入属于哪类，只输出 JSON：\n"
            '{"route": "chat|websearch|dev", "reason": "简短理由"}\n'
            "判定标准：\n"
            '- chat：闲聊、知识问答、概念解释（无需联网、无需写码）。\n'
            "- websearch：需要联网查最新信息（如最新版本、新闻、实时数据）。\n"
            "- dev：软件开发任务（新增/修改代码、加功能、改 bug、加工具函数）。\n"
            "只输出 JSON，不要加 markdown 代码块或多余文字。"
        )
        prompt = f"用户输入：{task}"

        route = "chat"
        reason = ""
        try:
            # 路由判定用同步 chat（非流式，结果一次性解析即可，无需逐 token）。
            # 但为支持前端看路由"思考"，仍走 stream_chat 推 reasoning。
            route_text = ""
            async for chunk in gateway.stream_chat(
                [ChatMessage("system", system), ChatMessage("user", prompt)],
                provider=provider, model=model, temperature=0.1,
            ):
                if chunk.delta:
                    route_text += chunk.delta
                # 路由判定一般无 reasoning_content（非 R1 模型），若有也透传。
                if chunk.reasoning:
                    _emit(emitter, {"type": "reasoning", "delta": chunk.reasoning})
            reason = route_text
            route, parsed_reason = _parse_route(route_text)
            if parsed_reason:
                reason = parsed_reason
        except Exception as exc:  # noqa: BLE001 - 路由失败降级到 chat
            route = "chat"
            reason = f"路由判定失败，降级到 chat：{exc!r}"

        _emit(emitter, {"type": "route", "route": route, "reason": reason})
        _done(emitter, NODE_ROUTER)
        return {
            "route": route,
            "route_reason": reason,
            "nodes_visited": visited,
        }

    return router_node


def _parse_route(text: str) -> tuple[str, str]:
    """从 LLM 输出解析 route 与 reason，容错处理。

    尽量从 JSON 抠出 route；解析失败时用关键词兜底匹配；再不行默认 chat。
    """
    # 尝试直接 JSON 解析（模型守规矩时）。
    try:
        data = json.loads(text.strip().strip("`").strip())
        if isinstance(data, dict) and data.get("route") in ("chat", "websearch", "dev"):
            return data["route"], data.get("reason", "")
    except Exception:  # noqa: BLE001
        pass
    # 退而求其次：正则抠 route 字段。
    import re
    m = re.search(r'"route"\s*:\s*"(chat|websearch|dev)"', text)
    if m:
        route = m.group(1)
        m2 = re.search(r'"reason"\s*:\s*"([^"]*)"', text)
        reason = m2.group(1) if m2 else ""
        return route, reason
    # 关键词兜底。
    low = text.lower()
    if "websearch" in low or "dev" in low and "cod" in low:
        if "websearch" in low:
            return "websearch", text[:100]
        return "dev", text[:100]
    return "chat", text[:100]


# ======================================================================
# Chat 节点：闲聊/问答，流式直答
# ======================================================================
def make_chat_node(gateway, emitter=None):
    """构造 chat 节点：绑 chat 模型，流式产出最终答案。

    流式：每个 token delta 经 emitter 推 {type:"token", delta}，前端逐字渲染；
    reasoning_content（R1 模型）推 {type:"reasoning", delta}，进思考折叠区。
    """

    async def chat_node(state: AgentGraphState) -> dict:
        visited = _visit(state, NODE_CHAT, emitter)
        kwargs = _build_chat_model_kwargs("chat")
        task = state.get("task", "")

        # 消费多轮历史（连续对话上下文）；messages 为空时降级单轮。
        msgs = _state_messages_full(state, task)
        answer = ""
        async for chunk in gateway.stream_chat(msgs, temperature=0.7, **kwargs):
            if chunk.delta:
                answer += chunk.delta
                _emit(emitter, {"type": "token", "delta": chunk.delta})
            if chunk.reasoning:
                _emit(emitter, {"type": "reasoning", "delta": chunk.reasoning})
        _done(emitter, NODE_CHAT)
        return {"final_answer": answer, "nodes_visited": visited}

    return chat_node


# ======================================================================
# WebSearch 节点：联网搜索 + 总结
# ======================================================================
def make_websearch_node(gateway, registry, emitter=None):
    """构造 websearch 节点：先搜索再总结，两步都流式可见。

    流程：
    1. 用 LLM 从用户输入提炼搜索词（若用户输入已是好查询词可直接用）。
    2. 调 WebSearchSkill.search 拿结果文本（不可用则降级提示）。
    3. 绑 websearch 模型，把结果 + 原始问题喂给 LLM 流式总结，逐 token 推前端。
    """

    async def websearch_node(state: AgentGraphState) -> dict:
        visited = _visit(state, NODE_WEBSEARCH, emitter)
        kwargs = _build_chat_model_kwargs("websearch")
        task = state.get("task", "")

        # 1) 提炼搜索词：让 LLM 把用户输入浓缩成搜索关键词。
        query = task
        try:
            kw_kwargs = _build_chat_model_kwargs("websearch")
            q_text = ""
            async for chunk in gateway.stream_chat(
                [ChatMessage("system", "把用户输入提炼成一个简短英文/中文搜索关键词，只输出关键词本身。"),
                 ChatMessage("user", task)],
                temperature=0.1, **kw_kwargs,
            ):
                if chunk.delta:
                    q_text += chunk.delta
            q_text = q_text.strip().strip("`").strip()
            if q_text and len(q_text) < len(task) * 2:
                query = q_text
        except Exception:  # noqa: BLE001 - 提炼失败用原 task 作查询
            query = task
        _emit(emitter, {"type": "step", "step": {"kind": "search_query", "query": query}})

        # 2) 搜索：从 registry 取 websearch skill。
        search_results = ""
        ws_skill = registry.get("websearch") if registry else None
        if ws_skill is not None:
            try:
                search_results = ws_skill.search(query)
                _emit(emitter, {"type": "step", "step": {
                    "kind": "search_results", "query": query,
                    "preview": search_results[:300],
                }})
            except Exception as exc:  # noqa: BLE001 - 搜索失败降级
                search_results = f"搜索失败：{exc!r}"
        else:
            search_results = "[websearch] WebSearch Skill 未注册。"

        # 3) 总结：把搜索结果 + 原始问题喂给 LLM 流式产出。
        # 保留对话历史（连续对话上下文），用增强 user（问题+搜索结果）替换当前 prompt。
        ws_system = (
            "你是 DevPilot 的联网搜索助手。基于搜索结果回答用户问题，"
            "若搜索结果不相关或不足，明确说明。回答要简洁、带来源依据。"
        )
        enhanced_user = (
            f"用户问题：{task}\n\n搜索结果：\n{search_results}\n\n请基于以上信息回答。"
        )
        hist = _history_messages(state)
        # 去掉末尾的当前 prompt（避免与增强版重复），保留更早的多轮历史。
        hist = hist[:-1] if hist and hist[-1].content == task else hist
        msgs = [ChatMessage("system", ws_system)] + hist + [ChatMessage("user", enhanced_user)]
        answer = ""
        async for chunk in gateway.stream_chat(msgs, temperature=0.4, **kwargs):
            if chunk.delta:
                answer += chunk.delta
                _emit(emitter, {"type": "token", "delta": chunk.delta})
            if chunk.reasoning:
                _emit(emitter, {"type": "reasoning", "delta": chunk.reasoning})
        _done(emitter, NODE_WEBSEARCH)
        return {
            "search_query": query,
            "search_results": search_results,
            "final_answer": answer,
            "nodes_visited": visited,
        }

    return websearch_node


# ======================================================================
# Dev 分支节点：研发闭环（planner→coder→reviewer→tester）
# 复用现有 agents.py 的逻辑，但改为绑模型的流式调用 + 推 blackboard 帧。
# ======================================================================
def make_planner_node(gateway, audit, emitter=None):
    """planner 节点：拆解任务为步骤，流式产出，写 state.plan。"""

    async def planner_node(state: AgentGraphState) -> dict:
        visited = _visit(state, NODE_PLANNER, emitter)
        kwargs = _build_chat_model_kwargs("planner")
        task = state.get("task", "")
        trace_id = state.get("messages", [""])[0] if state.get("messages") else ""

        system = (
            "你是资深研发流程拆解专家。把需求拆成 3-5 个可执行步骤，"
            "覆盖：定位代码→改代码→自测→提 PR。"
            "输出要求：每步一行纯文本，不要编号、不要 markdown 前缀、不要多余空行。"
        )
        prompt = f"需求：{task}\n请拆解为 3-5 个可执行步骤。"
        if audit is not None:
            audit.record("llm_call", "planner", {"prompt_preview": prompt[:200]}, trace_id=str(trace_id))

        raw = ""
        async for chunk in gateway.stream_chat(
            [ChatMessage("system", system), ChatMessage("user", prompt)],
            temperature=0.5, **kwargs,
        ):
            if chunk.delta:
                raw += chunk.delta
                _emit(emitter, {"type": "token", "delta": chunk.delta})
            if chunk.reasoning:
                _emit(emitter, {"type": "reasoning", "delta": chunk.reasoning})
        steps = [line.strip() for line in raw.splitlines() if line.strip()]
        if not steps:
            steps = [raw.strip()] if raw.strip() else [task]
        if audit is not None:
            audit.record("agent_step", "planner",
                         {"step": "plan_done", "num_steps": len(steps)}, trace_id=str(trace_id))
        _emit(emitter, {"type": "blackboard", "data": {"plan": steps}})
        _done(emitter, NODE_PLANNER)
        return {"plan": steps, "nodes_visited": visited}

    return planner_node


def make_coder_node(gateway, registry, audit, emitter=None):
    """coder 节点：据 plan 产出代码改动方案，流式产出，写 state.code_diff。"""

    async def coder_node(state: AgentGraphState) -> dict:
        visited = _visit(state, NODE_CODER, emitter)
        kwargs = _build_chat_model_kwargs("coder")
        task = state.get("task", "")
        plan = state.get("plan", [])
        trace_id = state.get("messages", [""])[0] if state.get("messages") else ""

        plan_text = "\n".join(f"- {s}" for s in plan) if plan else "(无拆解步骤，直接依据需求)"
        system = (
            "你是资深开发工程师。依据需求与拆解步骤，产出要做的代码改动方案。"
            "输出要求：先简述改动思路，再给出关键文件的伪 diff（用 ```diff 代码块），"
            "最后列出受影响文件。务必具体、可执行，避免空话。"
        )
        prompt = f"## 需求\n{task}\n\n## 拆解步骤\n{plan_text}\n\n请产出代码改动方案与伪 diff。"
        if audit is not None:
            audit.record("llm_call", "coder", {"prompt_preview": prompt[:200]}, trace_id=str(trace_id))

        code_diff = ""
        async for chunk in gateway.stream_chat(
            [ChatMessage("system", system), ChatMessage("user", prompt)],
            temperature=0.4, **kwargs,
        ):
            if chunk.delta:
                code_diff += chunk.delta
                _emit(emitter, {"type": "token", "delta": chunk.delta})
            if chunk.reasoning:
                _emit(emitter, {"type": "reasoning", "delta": chunk.reasoning})
        _emit(emitter, {"type": "blackboard", "data": {"code_diff": code_diff}})
        _done(emitter, NODE_CODER)
        return {"code_diff": code_diff, "nodes_visited": visited}

    return coder_node


def make_reviewer_node(gateway, audit, approval=None, emitter=None):
    """reviewer 节点：评审 code_diff，高危则走审批门。"""

    async def reviewer_node(state: AgentGraphState) -> dict:
        visited = _visit(state, NODE_REVIEWER, emitter)
        kwargs = _build_chat_model_kwargs("reviewer")
        diff = state.get("code_diff", "")
        trace_id = state.get("messages", [""])[0] if state.get("messages") else ""

        if not diff:
            review = "[reviewer] 无 code_diff 可审（Coder 阶段未产出），建议人工介入。"
            _emit(emitter, {"type": "blackboard", "data": {"review": review}})
            _done(emitter, NODE_REVIEWER)
            return {"review": review, "nodes_visited": visited}

        system = (
            "你是资深代码评审专家。评审给定的代码改动方案/diff，输出："
            "1) 风险点列表（每条一行，含严重度 高/中/低）；"
            "2) 一行结论：是否判定为高危（含不可逆/线上影响/安全/合规问题）。"
            "格式要求：最后单独一行写 'HIGH_RISK: yes' 或 'HIGH_RISK: no'。"
        )
        prompt = f"## 待评审改动\n{diff}\n\n请评审并给出风险点与高危判定。"
        if audit is not None:
            audit.record("llm_call", "reviewer", {"prompt_preview": prompt[:200]}, trace_id=str(trace_id))

        review = ""
        async for chunk in gateway.stream_chat(
            [ChatMessage("system", system), ChatMessage("user", prompt)],
            temperature=0.3, **kwargs,
        ):
            if chunk.delta:
                review += chunk.delta
                _emit(emitter, {"type": "token", "delta": chunk.delta})
            if chunk.reasoning:
                _emit(emitter, {"type": "reasoning", "delta": chunk.reasoning})
        _emit(emitter, {"type": "blackboard", "data": {"review": review}})

        # 高危判定 + 审批（沿用原 ReviewerAgent 逻辑）。
        high_risk = "high_risk: yes" in review.lower()
        approval_result = None
        if high_risk and approval is not None:
            action = "commit_and_pr"
            if approval.requires_approval(action):
                if audit is not None:
                    audit.record("approval", "reviewer",
                                 {"action": action, "reason_preview": review[:200]}, trace_id=str(trace_id))
                result = approval.request(
                    action=action,
                    args={"branch": "devpilot/auto", "message": state.get("task", "")[:200]},
                    reason=review,
                )
                approval_result = {
                    "approved": result.approved,
                    "comment": result.comment,
                    "modified_args": result.modified_args,
                }
                if audit is not None:
                    audit.record("approval", "reviewer",
                                 {"approved": result.approved, "comment": result.comment}, trace_id=str(trace_id))

        _done(emitter, NODE_REVIEWER)
        update: dict = {"review": review, "nodes_visited": visited}
        if approval_result is not None:
            update["approval"] = approval_result
        return update

    return reviewer_node


def make_tester_node(gateway, registry, audit, emitter=None):
    """tester 节点：调 cicd skill 触发流水线，无则降级提示，写 state.test_result。"""

    async def tester_node(state: AgentGraphState) -> dict:
        visited = _visit(state, NODE_TESTER, emitter)
        trace_id = state.get("messages", [""])[0] if state.get("messages") else ""

        cicd = registry.get("cicd") if registry else None
        if cicd is None:
            test_result = "[tester] CI Skill 未配置（registry 无 'cicd'），跳过测试。"
            if audit is not None:
                audit.record("agent_step", "tester", {"step": "tester_skipped_no_skill"}, trace_id=str(trace_id))
            _emit(emitter, {"type": "blackboard", "data": {"test_result": test_result}})
            _done(emitter, NODE_TESTER)
            return {"test_result": test_result, "nodes_visited": visited}

        import os
        job = os.getenv("DEVPILOT_CI_JOB", "devpilot-demo")
        try:
            if audit is not None:
                audit.record("tool_call", "tester", {"tool": "cicd.trigger_pipeline", "job": job}, trace_id=str(trace_id))
            trigger_out = cicd.trigger_pipeline(job=job, params={})
        except NotImplementedError:
            test_result = "[tester] cicd.trigger_pipeline 未实现，CI Skill 未配置。"
            _emit(emitter, {"type": "blackboard", "data": {"test_result": test_result}})
            _done(emitter, NODE_TESTER)
            return {"test_result": test_result, "nodes_visited": visited}
        except Exception as exc:  # noqa: BLE001
            test_result = f"[tester] trigger_pipeline 失败: {exc}"
            _emit(emitter, {"type": "blackboard", "data": {"test_result": test_result}})
            _done(emitter, NODE_TESTER)
            return {"test_result": test_result, "nodes_visited": visited}

        run_id = ""
        if "queue=" in trigger_out:
            after = trigger_out.split("queue=", 1)[1].strip()
            run_id = "queue/" + after.split()[0].rstrip("/") if after else ""
        report = ""
        if run_id:
            try:
                if audit is not None:
                    audit.record("tool_call", "tester",
                                 {"tool": "cicd.fetch_test_report", "run_id": run_id}, trace_id=str(trace_id))
                report = cicd.fetch_test_report(run_id=run_id)
            except NotImplementedError:
                report = "[tester] cicd.fetch_test_report 未实现。"
            except Exception as exc:  # noqa: BLE001
                report = f"[tester] fetch_test_report 失败: {exc}"

        test_result = (
            f"=== trigger ===\n{trigger_out}\n\n"
            f"=== test report (run_id={run_id or 'n/a'}) ===\n{report}"
        )
        if audit is not None:
            audit.record("agent_step", "tester",
                         {"step": "tester_done", "result_len": len(test_result)}, trace_id=str(trace_id))
        _emit(emitter, {"type": "blackboard", "data": {"test_result": test_result}})
        _done(emitter, NODE_TESTER)
        return {"test_result": test_result, "nodes_visited": visited}

    return tester_node
