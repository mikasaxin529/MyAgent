"""动态编排节点：plan-and-execute 模式。

与 nodes.py 的"固定分支"不同，这里 planner 根据用户输入**动态生成** N 个
执行步骤，executor 循环逐个执行——真正实现"模型根据用户输入动态编排节点"。

流程：
    planner  →  生成 plan_steps（每步含 name/description/output/needs_search）
              →  推 {type:"plan", steps} 给前端动态画图
    executor ↔  循环：执行 steps[step_index]，needs_search 则先联网，
              把前一步产出作上下文调 LLM 流式产出，写回 step_results
              step_index < len(steps) → 继续 executor，否则 END

为什么这是"动态编排"而非"固定分支"：
- 节点数量、名称、职责由 LLM 据输入规划，非硬编码。
- "总结7月AI资讯"→3步、"写个工具函数"→单步直答，结构随任务而变。
- 前端图形流据 plan 帧动态生成节点图，而非展示预设的 7 节点。
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..config import load_agent_models
from ..gateway import ChatMessage
from .nodes import _history_messages, _recent_history_text, _state_messages_full, _visit, _done
from .state import AgentGraphState


def _build_tool_catalog(registry) -> str:
    """序列化 registry 所有 SkillSpec 为 TOOL_CATALOG 文本，注入 planner prompt。

    每个工具一行 JSON：name/description/guidance(厚描述，what/when/when-not)/parameters。
    让模型读描述自决选工具（学 ChatFlow GUIDANCE 注入），而非靠 few-shot 示例。
    """
    if registry is None:
        return "（未注册工具）"
    import json
    lines = []
    for spec in registry.all_specs():
        lines.append(json.dumps({
            "name": spec.name,
            "description": spec.description,
            "guidance": spec.guidance or "",
            "parameters": spec.schema,
        }, ensure_ascii=False))
    return "\n".join(lines) or "（无工具）"


def _emit(emitter, frame: dict) -> None:
    if emitter is None:
        return
    try:
        emitter(frame)
    except Exception:  # noqa: BLE001
        pass


def _step_id(i: int) -> str:
    """生成步骤节点 id，与前端图形流节点 id 对齐。"""
    return f"step{i+1}"


# ======================================================================
# 搜索健壮性辅助：限定词保底 + trust_memory 推断 + 结果质量判断
# 设计目标：让动态编排对任意搜索查询都稳健，不因 planner 偶发漏带
# 限定词或误标 trust_memory 而跑偏（如"四川"丢失→召回全国各省）。
# ======================================================================

# 中国省/直辖市/自治区名，用于限定词保底。
_PROVINCES = (
    "北京", "上海", "天津", "重庆", "四川", "广东", "江苏", "浙江",
    "山东", "河南", "河北", "湖北", "湖南", "福建", "安徽", "江西",
    "辽宁", "吉林", "黑龙江", "陕西", "山西", "广西", "云南", "贵州",
    "甘肃", "青海", "海南", "内蒙古", "新疆", "西藏", "宁夏",
)

# 强时效词：出现则 trust_memory 推断为 False（防幻觉）；其余默认 True。
_TIME_WORDS = (
    "最新", "最近", "本月", "今天", "今日", "刚刚", "实时", "眼下",
    "新闻", "资讯", "动态", "发生", "现在",
)


def _ensure_limiters(query: str, task: str) -> str:
    """保底：若 search_query 丢了 task 里的年份/省份限定词，补回去。

    防止 planner 偶发漏带限定词导致召回跑偏——如"四川分数线"丢"四川"
    后 Tavily 会召回全国各省分数线，外省学校混入结果。
    """
    q = query or ""
    # 年份（20xx）：task 里有但 query 里没有 → 追加。
    for y in set(re.findall(r"20\d{2}", task or "")):
        if y not in q:
            q = f"{q} {y}".strip()
    # 省份：task 里有但 query 里没有 → 追加。
    for prov in _PROVINCES:
        if prov in (task or "") and prov not in q:
            q = f"{q} {prov}".strip()
    return q or query


def _infer_trust_memory(task: str) -> bool:
    """planner 未标注 trust_memory 时的自动推断。

    强时效新闻/资讯类 → False（只能用搜索结果，防幻觉）；
    其余（含分数线/名单/数据等客观数据查询）→ True（优先采信搜索，允许知识补充+纠偏）。
    """
    for w in _TIME_WORDS:
        if w in (task or ""):
            return False
    return True


def _is_degraded(result: str) -> bool:
    """搜索结果是否为降级提示（而非真实命中）。"""
    return bool(result) and result.startswith("[websearch]")


def _result_count(result: str) -> int:
    """粗估搜索结果有效条数（按 Tavily 拼接格式的序号计数）。"""
    return len(re.findall(r"^\d+\.\s", result or "", re.MULTILINE))


# ======================================================================
# Classifier 节点：意图分流（简单对话 vs 需要规划）
# 用 ollama 快速二分类——普通对话直接 ollama 直答，复杂任务才走
# deepseek 规划+执行。省掉简单对话的规划开销，强模型专注复杂规划。
# 任何失败默认 complex（走 deepseek 规划，最稳兜底）。
# ======================================================================
def make_classifier_node(gateway, emitter=None):
    """classifier：用 LLM 判意图，输出 intent ∈ {simple, complex}。

    simple=闲聊/知识问答/概念解释/单轮直答能搞定；
    complex=需联网搜索/多步处理/写代码/总结多源。
    """

    async def classifier_node(state: AgentGraphState) -> dict:
        visited = _visit(state, "classifier", emitter)
        task = state.get("task", "")

        models = load_agent_models()
        provider, model = models.get("classifier", ("deepseek", "deepseek-chat"))

        system = (
            "你是 DevPilot 的意图分类器。判断用户输入属于哪类，只输出 JSON：\n"
            '{"intent": "simple|complex", "reason": "简短理由"}\n'
            "判定标准：\n"
            "- simple：闲聊、问候、单轮知识问答、概念解释、简单计算——"
            "一句话能直接回答、无需联网搜索、无需多步处理、无需写代码。\n"
            "- complex：需要联网搜索最新/外部信息、需要查询实时数据"
            "（如天气/股价/新闻）、需要多步推理与加工"
            "（如总结多源资讯、对比分析）、需要写或改代码、需要规划执行流程。\n"
            "只输出 JSON，不要加 markdown 代码块或多余文字。"
        )
        prompt = f"用户输入：{task}"

        intent = "complex"  # 默认复杂（兜底走 deepseek 规划最稳）
        reason = ""
        try:
            raw = ""
            async for chunk in gateway.stream_chat(
                [ChatMessage("system", system), ChatMessage("user", prompt)],
                provider=provider, model=model, temperature=0.1,
            ):
                if chunk.delta:
                    raw += chunk.delta
                if chunk.reasoning:
                    _emit(emitter, {"type": "reasoning", "delta": chunk.reasoning})
            intent, reason = _parse_intent(raw)
        except Exception as exc:  # noqa: BLE001 - 分类失败兜底 complex
            intent = "complex"
            reason = f"意图分类失败，降级到 complex：{exc!r}"

        _emit(emitter, {"type": "node", "node_id": "classifier", "status": "done"})
        return {"intent": intent, "nodes_visited": visited}

    return classifier_node


def _parse_intent(text: str) -> tuple[str, str]:
    """从 LLM 输出解析 intent 与 reason，容错处理。

    尽量从 JSON 抠出 intent；解析失败时正则兜底；再不行默认 complex。
    """
    import json as _json
    t = (text or "").strip().strip("`").strip()
    # 去 ```json 包裹。
    t = re.sub(r"^json\s*", "", t, flags=re.IGNORECASE)
    try:
        data = _json.loads(t)
        if isinstance(data, dict) and data.get("intent") in ("simple", "complex"):
            return data["intent"], data.get("reason", "")
    except Exception:  # noqa: BLE001
        pass
    # 正则兜底。
    m = re.search(r'"intent"\s*:\s*"(simple|complex)"', t, re.IGNORECASE)
    if m:
        intent = m.group(1).lower()
        m2 = re.search(r'"reason"\s*:\s*"([^"]*)"', t)
        return intent, (m2.group(1) if m2 else "")
    # 关键词兜底。
    low = t.lower()
    if "simple" in low and "complex" not in low:
        return "simple", t[:100]
    if "complex" in low:
        return "complex", t[:100]
    return "complex", t[:100]


# ======================================================================
# Direct chat 节点：简单对话直接 ollama 流式直答（不规划）
# ======================================================================
def make_direct_chat_node(gateway, emitter=None):
    """direct_chat：绑 chat 模型（ollama），消费多轮历史，流式产出 final_answer。

    与 planner→executor 路径的区别：跳过规划，单次 LLM 调用直答，
    token 逐字推前端，不推 plan 帧（前端不画步骤图）。
    """

    async def direct_chat_node(state: AgentGraphState) -> dict:
        visited = _visit(state, "chat", emitter)
        models = load_agent_models()
        provider, model = models.get("chat", ("deepseek", "deepseek-chat"))
        task = state.get("task", "")

        # 消费多轮历史（连续对话上下文）；messages 为空时降级单轮。
        msgs = _state_messages_full(state, task)
        answer = ""
        async for chunk in gateway.stream_chat(
            msgs, provider=provider, model=model, temperature=0.7,
        ):
            if chunk.delta:
                answer += chunk.delta
                _emit(emitter, {"type": "token", "delta": chunk.delta})
            if chunk.reasoning:
                _emit(emitter, {"type": "reasoning", "delta": chunk.reasoning})
        _done(emitter, "chat")
        return {"final_answer": answer, "nodes_visited": visited}

    return direct_chat_node


def _route_intent(state: AgentGraphState) -> str:
    """classifier 后条件边：simple → direct（ollama 直答）；complex → planner（deepseek 规划）。

    intent 未设置（classifier 失败兜底）时默认 complex 走 planner。
    """
    if state.get("intent") == "simple":
        return "direct"
    return "planner"


# ======================================================================
# Planner 节点：动态生成步骤计划
# ======================================================================
def make_planner_node(gateway, registry=None, emitter=None):
    """planner：LLM 据用户输入 + 工具清单生成结构化步骤计划。

    schema/不变量驱动，零 few-shot（学 ChatFlow planner：靠工具描述 + 不变量
    而非硬编码用户输入示例）。注入今天日期供相对日期解析为绝对日期。
    输出 plan_steps: [{id,name,description,output,needs_search,tool,args,date,trust_memory}]。
    解析失败/简单闲聊 → 降级为单步直答（保证总能跑通）。
    """

    async def planner_node(state: AgentGraphState) -> dict:
        _emit(emitter, {"type": "node", "node_id": "planner", "status": "running"})
        task = state.get("task", "")
        models = load_agent_models()
        provider, model = models.get("planner", ("deepseek", "deepseek-chat"))

        from datetime import date as _date
        today = _date.today().strftime("%Y年%m月%d日")
        tools_json = _build_tool_catalog(registry)

        system = (
            "你是 DevPilot 的通用任务规划器。读取 <TOOL_CATALOG> 中每个工具的 "
            "name/guidance/parameters，依据用户意图产出结构化步骤计划。"
            "不要记住任何具体查询样例，仅凭工具描述的 capability 做判断。\n\n"
            "<ENV_CONTEXT>\n今天是 " + today + "。这是相对日期（今天/明天/后天/下周X）"
            "解析为绝对日期的唯一基准。\n</ENV_CONTEXT>\n\n"
            "<TOOL_CATALOG>\n" + tools_json + "\n</TOOL_CATALOG>\n\n"
            "<PLANNER_INVARIANTS>\n你是规划器，只规划不执行、不产出内容（地图不画房子）。"
            "一个合格的步骤满足：\n"
            "1. 原子承诺：该步恰好多一个可指认产物（如天气原始数据、天气报告）。\n"
            "2. 职责单一：一步只做一类事。\n"
            "3. 可验证交付：产出可被下一步引用或交付用户。\n"
            "步骤关系：正交、依赖显式化（基于步骤N的…）、同类动作一次性。\n"
            "description 严禁出现具体产物内容（答案/代码/完整文案），只描述要做什么。\n"
            "</PLANNER_INVARIANTS>\n\n"
            "<DATE_RULES>\n涉及相对日期（今天/明天/后天/下周X/N号）的步骤："
            "必须先据 <ENV_CONTEXT> 的今天日期，把相对日期解析为 ISO 8601 绝对日期 "
            "YYYY-MM-DD，写进该步 args.date 与 description。禁止把明天原样透传给工具。\n"
            "</DATE_RULES>\n\n"
            "<TOOL_RULES>\n"
            "- 有专用工具能完成的步骤：tool 填工具名（weather_current/weather_forecast/"
            "websearch 等），args 填该工具 parameters 要求的参数。有专用工具时不要走通用 websearch。\n"
            "- 需联网获取最新/外部信息且无专用工具：tool=websearch，args.query 填精炼搜索词"
            "（含解析后的绝对日期/省份等限定词，多用空格分词）。\n"
            "- 纯推理/整理/总结/闲聊：tool 留空，needs_search=false。\n"
            "- 必填参数缺失不要臆造，产出 name=追问 的步骤向用户追问。\n"
            "- 自带精炼输出的专用工具（weather_current/weather_forecast 等）："
            "工具一次调用能完成的任务只产 1 步直返，不额外加总结步。\n"
            "- 返回原始网页/需整合的工具（websearch）：必须至少 2 步——"
            "①取数步(tool=websearch,needs_search=true) ②整合呈现步"
            "(tool 空,needs_search=false,trust_memory=false)。不得以 websearch 步"
            "作为最后一步直返网页原文（会复读网页）。仅当用户明确要多类信息才拆更多步。\n"
            "</TOOL_RULES>\n\n"
            "<OUTPUT_SCHEMA>\n严格输出纯 JSON（不要 markdown 代码块）：\n"
            '{"steps":[{"name":"2-6字","description":"这步要做什么（含解析后的绝对日期）",'
            '"output":"本步产出什么","needs_search":false,"tool":"工具名或空",'
            '"args":{},"trust_memory":false}]}\n'
            "args 例：weather_forecast → {\"location\":\"成都\",\"date\":\"2026-08-08\"}；"
            "websearch → {\"query\":\"成都 2026-08-08 天气\"}；纯总结步 → {}。\n"
            "trust_memory：时效资讯/新闻=false（防幻觉）；客观数据/常识=true。\n"
            "简单闲聊也输出 1 步（tool 留空,needs_search=false,直答）。只输出 JSON。"
            "</OUTPUT_SCHEMA>"
        )
        prompt = "<USER_REQUEST>\n" + task + "\n</USER_REQUEST>"
        hist_text = _recent_history_text(state, 4)
        if hist_text:
            prompt += f"\n\n[对话历史]\n{hist_text}"

        raw = ""
        async for chunk in gateway.stream_chat(
            [ChatMessage("system", system), ChatMessage("user", prompt)],
            provider=provider, model=model, temperature=0.2,
        ):
            if chunk.delta:
                raw += chunk.delta
            if chunk.reasoning:
                _emit(emitter, {"type": "reasoning", "delta": chunk.reasoning})

        plan_steps = _parse_plan(raw, task)
        # 给每步补 id（前端图形流节点 id 对齐）。
        for i, s in enumerate(plan_steps):
            s.setdefault("id", _step_id(i))

        _emit(emitter, {"type": "plan", "steps": plan_steps})
        _emit(emitter, {"type": "node", "node_id": "planner", "status": "done"})
        return {"plan_steps": plan_steps, "step_index": 0, "step_results": []}

    return planner_node


def _parse_plan(raw: str, task: str) -> list[dict]:
    """从 LLM 输出解析步骤计划，容错降级。

    解析失败 → 退化为单步直答（保证总能继续执行）。
    """
    text = raw.strip().strip("`").strip()
    # 去掉可能的 ```json 包裹。
    text = re.sub(r"^json\s*", "", text, flags=re.IGNORECASE)
    try:
        data = json.loads(text)
        steps = data.get("steps") if isinstance(data, dict) else None
        if isinstance(steps, list) and steps:
            # 规范化每步字段。
            norm = []
            for i, s in enumerate(steps):
                if not isinstance(s, dict):
                    continue
                norm.append({
                    "id": _step_id(i),
                    "name": str(s.get("name", f"步骤{i+1}"))[:20],
                    "description": str(s.get("description", ""))[:400],
                    "output": str(s.get("output", ""))[:200],
                    "needs_search": bool(s.get("needs_search", False)),
                    # tool：标记该步用哪个工具（weather_current/weather_forecast/websearch...）。
                    "tool": str(s.get("tool", "") or "")[:20],
                    # args：工具参数对象（如 {"location":"成都","date":"2026-08-08"}）。
                    "args": s.get("args") if isinstance(s.get("args"), dict) else {},
                    # search_query：兼容兜底，executor 工具调用失败时用它换 websearch。
                    "search_query": str(s.get("search_query", ""))[:200],
                    # trust_memory：保留 planner 原值（可能 None），executor 侧兜底推断。
                    "trust_memory": s.get("trust_memory", None),
                })
            if norm:
                return norm
    except Exception:  # noqa: BLE001
        pass
    # 降级：单步直答。
    return [{
        "id": _step_id(0),
        "name": "直接回答",
        "description": task,
        "output": "对用户的直接回答",
        "needs_search": False,
    }]


# ======================================================================
# Executor 节点：循环执行每一步
# ======================================================================
async def _synthesize_final(gateway, provider, model, task, step_name,
                            step_desc, materials, emitter, idx) -> str:
    """把搜索取数的原始资料整合成面向用户的丰富答案。

    websearch 返回网页原文 dump，直接当 final_answer 会复读网页。此函数用
    LLM 把资料提炼成分段清晰、关键数据表格化、标注来源的答案。学 ChatFlow
    总结步的"记录者"职责：基于资料加工，不臆造。
    """
    system = (
        "你是 DevPilot 的整合呈现节点。基于下方【检索资料】加工出面向用户的最终答案。\n"
        "**采信规则**：优先采信资料；可结合自身知识补充，但资料与之冲突以资料为准；"
        "与查询主题约束明显不符的条目（如限定某地/某日却混入其他）应剔除并简要说明，"
        "不可张冠李戴。\n"
        "**输出格式**：分段清晰，关键信息用表格或列表呈现，每个判断引用具体资料来源，"
        "结尾标注来源。直接给答案，不要加'根据资料'之类前缀。"
    )
    prompt = (
        f"【用户问题】{task}\n\n"
        f"【当前步骤】{step_name} - {step_desc}\n\n"
        f"【检索资料】\n{materials}\n\n"
        "请把以上资料整合成一份结构清晰、信息丰富、面向用户的答案。"
    )
    msgs = [ChatMessage("system", system), ChatMessage("user", prompt)]
    out = ""
    async for chunk in gateway.stream_chat(
        msgs, provider=provider, model=model, temperature=0.4,
    ):
        if chunk.delta:
            out += chunk.delta
            _emit(emitter, {"type": "token", "delta": chunk.delta, "step": idx})
        if chunk.reasoning:
            _emit(emitter, {"type": "reasoning", "delta": chunk.reasoning})
    return out


def make_executor_node(gateway, registry, emitter=None):
    """executor：执行 plan_steps[step_index]，循环驱动。

    每步：
    1. needs_search → 调 WebSearchSkill 取外部信息作上下文。
    2. 把前一步产出 + 当前步 description/output 喂给 LLM 流式产出。
    3. 产出存 step_results[step_index]；最后一步产出即 final_answer。
    4. 推 node 帧更新该步骤节点状态（running→done）。
    """

    async def executor_node(state: AgentGraphState) -> dict:
        task = state.get("task", "")
        steps = state.get("plan_steps", [])
        idx = state.get("step_index", 0)
        results = list(state.get("step_results", []))

        if idx >= len(steps):
            return {"step_index": idx}  # 防御：越界直接结束

        step = steps[idx]
        sid = step.get("id", _step_id(idx))
        name = step.get("name", f"步骤{idx+1}")
        desc = step.get("description", "")
        out_spec = step.get("output", "")
        needs_search = step.get("needs_search", False)

        _emit(emitter, {"type": "node", "node_id": sid, "status": "running"})
        _emit(emitter, {"type": "step", "step": {
            "kind": "step_start", "index": idx, "name": name,
            "description": desc, "needs_search": needs_search,
        }})

        models = load_agent_models()
        provider, model = models.get("coder", ("deepseek", "deepseek-chat"))

        tool = step.get("tool", "") or ""
        args = step.get("args") or {}
        # ---- 分支 A：工具调用步骤，据 step.tool 动态分发 ----
        # 不再硬编码 weather/websearch：据 step.tool 从 registry.find_spec 取工具，
        # 调 spec.func(**step.args)。新工具零改 executor（学 ChatFlow GUIDANCE 自决）。
        # 触发条件：needs_search=true（联网）或 tool 非空（专用工具）——
        # 天气等专用工具步 planner 可能标 needs_search=false（非 search 语义），
        # 故据 tool 判断而非仅 needs_search，否则专用工具步会被误走总结分支。
        # tool 留空/未注册/调用失败 → 降级 websearch（用 args.query 或 search_query）。
        if needs_search or tool:
            result = None
            if tool and registry is not None:
                spec = registry.find_spec(tool)
                if spec is not None:
                    try:
                        result = spec.func(**args)
                    except TypeError:
                        # 参数名不对齐：降级 websearch 兜底。
                        result = None
                    except Exception as exc:  # noqa: BLE001
                        result = f"[{tool}] 工具调用失败：{exc!r}"
            if result is None:
                # 降级 websearch：用 args.query 或 search_query 或 desc。
                ws_skill = registry.get("websearch") if registry else None
                query = args.get("query") or step.get("search_query") or desc
                # 限定词保底：若 query 丢了 task 里的年份/省份，补回——
                # 防 planner 漏带导致召回跑偏（"四川"丢失→全国各省混入）。
                query = _ensure_limiters(query, task)
                if ws_skill is not None:
                    try:
                        result = ws_skill.search(query, max_results=8, time_range="month")
                    except Exception as exc:  # noqa: BLE001
                        result = f"[websearch] 搜索失败：{exc!r}"
                    # 补搜循环：结果为降级提示或有效条数过少时，换 desc 再搜一次聚合。
                    if _is_degraded(result) or _result_count(result) < 3:
                        try:
                            alt = ws_skill.search(desc, max_results=8, time_range="month")
                            if not _is_degraded(alt) and _result_count(alt) > 0:
                                result = alt if _is_degraded(result) else f"{result}\n\n{alt}"
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    result = f"[{tool or 'websearch'}] 工具未注册/未找到。"
            # 推工具结果快照给前端思考区（preview）。
            _emit(emitter, {"type": "step", "step": {
                "kind": "search_results", "index": idx,
                "preview": result[:600],
            }})
            results.append(result)
            _emit(emitter, {"type": "node", "node_id": sid, "status": "done"})
            update = {"step_index": idx + 1, "step_results": results}
            if idx + 1 >= len(steps):
                # 最后一步的 final_answer：
                # - 搜索取数步（needs_search=true）：websearch 返回网页原文 dump，
                #   直接当答案会复读网页。追加 LLM 整合成面向用户的丰富答案
                #   （分段/表格/来源），学 ChatFlow 总结步的整合职责。这是兜底
                #   保险——即便 planner 产单步搜索也会整合，不依赖模型守规则。
                # - 专用工具步（weather 等 needs_search=false）：自带精炼结构化
                #   文本，直返即可，不再多过一次 LLM。
                if needs_search:
                    update["final_answer"] = await _synthesize_final(
                        gateway, provider, model, task, name, desc, result,
                        emitter, idx)
                else:
                    update["final_answer"] = result
            return update

        # ---- 分支 B：总结/处理步骤由 DeepSeek 执行 ----
        # 消息隔离（学 ChatFlow _build_focused_step_messages）：重建消息集，只给
        # 总目标 + 已完成步骤摘要 + 当前步指令，剥离完整对话历史，防模型提前
        # 执行后续步骤或越界生成最终产物。
        # trust_memory 分级约束 + 输出格式要求（分段/表格/引用数据/标注来源）。
        tm = step.get("trust_memory")
        if tm is None:
            tm = _infer_trust_memory(task)
        elif isinstance(tm, str):
            tm = tm.lower() == "true"
        trust_memory = bool(tm)
        prev_context = ""
        if results:
            prev_context = "\n\n".join(
                f"【步骤{i+1}产出】\n{r}" for i, r in enumerate(results)
            )
        if trust_memory:
            mem_rule = (
                "**采信规则**：优先采信下方【前序步骤产出】中的资料；"
                "可结合自身知识补充，但资料与之冲突时以资料为准。"
                "与查询主题常识约束冲突的条目（如限定四川却混入外省）"
                "应剔除并简要说明，不可张冠李戴。"
            )
        else:
            mem_rule = (
                "**重要约束**：只能依据下方【前序步骤产出】中的信息作答，"
                "不得使用自身训练记忆（可能已过时）。资料不足明确说明"
                "'资料中未涉及'，绝不臆造或用记忆补充。"
            )
        system = (
            "你是 DevPilot 的总结/处理节点，负责基于已收集的资料加工输出。\n"
            + mem_rule + "\n"
            "**输出格式**：分段清晰，关键数据用表格或列表呈现，每个判断"
            "引用具体数据来源，结尾标注数据来源。不要加多余前缀，直接给本步产出。"
        )
        user_parts = [f"【总目标】{task}", f"【当前步骤】{name}",
                      f"【职责】{desc}", f"【本步应产出】{out_spec}"]
        if prev_context:
            user_parts.append(f"【前序步骤产出】\n{prev_context}")
        else:
            # 无前序资料的非搜索步骤（如纯推理）：可正常作答。
            user_parts.append("【前序步骤产出】（无，本步为首步或独立推理步骤）")
        prompt = "\n\n".join(user_parts)

        # 消息隔离：只给 system + 当前步聚焦 prompt，不带完整对话历史
        # （多步任务通常单轮复杂任务，防模型看到最终目标后越界提前产出）。
        msgs = [ChatMessage("system", system), ChatMessage("user", prompt)]
        result = ""
        async for chunk in gateway.stream_chat(
            msgs,
            provider=provider, model=model, temperature=0.4,
        ):
            if chunk.delta:
                result += chunk.delta
                _emit(emitter, {"type": "token", "delta": chunk.delta, "step": idx})
            if chunk.reasoning:
                _emit(emitter, {"type": "reasoning", "delta": chunk.reasoning})

        results.append(result)
        _emit(emitter, {"type": "node", "node_id": sid, "status": "done"})

        # 最后一步产出即最终答案。
        update: dict = {
            "step_index": idx + 1,
            "step_results": results,
        }
        if idx + 1 >= len(steps):
            update["final_answer"] = result
        return update

    return executor_node


# ======================================================================
# 条件边：executor 后判断是否还有步骤
# ======================================================================
def should_continue(state: AgentGraphState) -> str:
    """executor 完成后：还有步骤 → 继续 executor；否则 → END。

    返回 "executor" 或 "END"（langgraph 条件边的节点名/__end__）。
    """
    steps = state.get("plan_steps", [])
    idx = state.get("step_index", 0)
    if idx < len(steps):
        return "executor"
    return "__end__"
