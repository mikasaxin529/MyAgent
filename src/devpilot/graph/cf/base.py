"""ChatFlow 式节点基类与共享辅助。

节点用 emitter 回调推 ChatFlow 帧（thinking/content/route/plan/tool_call/
tool_result/search_item/reflection），SSE 端点 drain 队列序列化为 data:{json}\n\n。
对齐 ChatFlow graph/nodes/base.py 的 emit_thinking 与 _THINK_PREFIX 分流。
"""
from __future__ import annotations

from datetime import date as _date

from ...gateway import ChatMessage

# 思考/正文分流哨兵（对齐 ChatFlow _THINK_PREFIX）。
# call_model 节点把 reasoning 片段标 phase="reasoning"，正文标 phase="content"，
# 前端 thinkingSegments 按 (node, step_index, phase) 三元组累积，phase 区分两 part。
_THINK_PREFIX = "\x00THINK\x00"


def emit_thinking(emitter, node: str, phase: str, delta: str,
                   step_index: int | None = None) -> None:
    """推 thinking 帧给前端思考折叠区。

    node 决定思考块标题（前端 NODE_LABEL 映射：route_model→路由判断 /
    planner→规划 / call_model→推理 / call_model_after_tool→综合推理 /
    reflector→反思）。phase 区分 reasoning（推理链）与 content（思考正文）。
    step_index 非空时归属某步骤，空则消息级。
    """
    if not delta or emitter is None:
        return
    frame: dict = {"type": "thinking", "node": node, "phase": phase, "delta": delta}
    if step_index is not None:
        frame["step_index"] = step_index
    emit(emitter, frame)


def emit(emitter, frame: dict) -> None:
    """安全推一帧给 web 层（emitter 可能为 None，CLI 路径）。"""
    if emitter is None:
        return
    try:
        emitter(frame)
    except Exception:  # noqa: BLE001
        pass


def visit(state, node_id: str, emitter=None) -> list:
    """节点进入：推 node running 帧，返回更新后的 nodes_visited。"""
    visited = list(state.get("nodes_visited") or [])
    if node_id not in visited:
        visited.append(node_id)
    emit(emitter, {"type": "node", "node_id": node_id, "status": "running"})
    return visited


def done(emitter, node_id: str) -> None:
    """节点完成：推 node done 帧。"""
    emit(emitter, {"type": "node", "node_id": node_id, "status": "done"})


def step_id(i: int) -> str:
    """步骤节点 id，与前端图形流对齐。"""
    return f"step{i+1}"


def display_mode_for(tool_name: str) -> str:
    """工具的前端展示模式（对齐 ChatFlow ToolDisplayMode）。

    - websearch → "sources"（搜索结果卡片 + url 列表）
    - websearch_fetch_page → "fetch"（抓取状态卡片）
    - 其余（weather 等）→ "text"（纯文本结果）
    """
    if tool_name == "websearch":
        return "sources"
    if tool_name == "websearch_fetch_page":
        return "fetch"
    return "text"


def build_tools(registry, route: str):
    """按 route 决定给 call_model 绑哪些工具（OpenAI tools 格式）。

    search/search_code/finance 路由绑 websearch + weather + fetch_page；
    code/chat 不绑工具（直接答/写码）。返回 None 表示不绑。
    """
    from .route_node import ROUTE_MODEL_MAP  # 延迟 import 避免循环
    from ...skills.schema_normalize import to_openai_tools

    _key, _akey, bind = ROUTE_MODEL_MAP.get(route, ROUTE_MODEL_MAP["chat"])
    if not bind or registry is None:
        return None
    wanted = {"websearch", "weather_current", "weather_forecast", "websearch_fetch_page"}
    specs = [s for s in registry.all_specs() if s.name in wanted]
    return to_openai_tools(specs) or None


def system_prompt_with_date() -> str:
    """带今日日期的 system prompt。

    避免模型（训练截止早于当前日期）把搜索结果里的近期日期当“未来数据”剔除——
    明确告知今天日期，并声明搜索结果中的日期都是真实检索日期，不得剔除。
    同时引导相对时间词（最新/近期/本月/上个月）以今天为基准。

    排版约束：回答默认用 Markdown 结构化（正文短段落 + 要点列表 +
    关键词加粗），避免大段平铺文字；但闲聊/问候等一句话能说清的就直答，
    不为排版而排版。涉及流程、架构、状态流转、类关系等结构性内容时，
    用 ```mermaid 围栏画图（flowchart/sequenceDiagram 等），比长段文字直观。
    """
    today = _date.today().strftime("%Y年%m月%d日")
    return (
        f"你是 DevPilot 的助手。今天是 {today}。"
        "用户询问“最新/近期/本月/上个月/最近”等相对时间时，以今天为基准计算。"
        "联网搜索结果中出现的日期都是真实检索日期，近期日期（今年或去年的）"
        "均为有效信息，严禁以“未来日期”为由剔除搜索结果。\n"
        "回答的排版要求：\n"
        "- 问候、简单事实、一句话能答的 → 直接答，不套结构\n"
        "- 两点以上并行内容 → 用「- 」列表，每条一行，句首关键词**加粗**\n"
        "- 步骤/流程 → 有序编号列表\n"
        "- 每个自然段不超过 3 行；段落之间留空行\n"
        "- 代码用 ``` 围栏并标语言\n"
        "- 流程/架构/状态流转/时序 → 用 ```mermaid 围栏画图（如 flowchart TD、"
        "sequenceDiagram），配一两句话说明，不要再用大段文字复述图中已有的信息\n"
        "- mermaid 图严格遵循合法语法：节点标签含括号/引号/冒号/中文标点等特殊字符时"
        "必须用双引号包裹，如 A[\"步骤（一）\"]；边标签写在线上 |标签|；"
        "禁止用箭头字符/缩进画 ASCII 示意图，图只能画在 mermaid 围栏里\n"
        "专业、克制，不为排版而排版。"
    )


# SYSTEM_CHAT 原文（ensure_date_system 判断“首条是否纯占位”用）。
SYSTEM_CHAT_PLAIN = "你是 DevPilot 的助手，简洁专业地回答用户。"


def ensure_date_system(msgs):
    """确保消息列表首条是带今日日期的 system prompt。

    兼容 list[dict]（OpenAI 格式）与 list[ChatMessage] 两种形态：首条若为 system
    则替换为日期版，否则前插一条。返回新列表。

    首条 system 的附加段（长期记忆 [用户长期记忆] 等）不丢——拼在日期版
    prompt 之后，保证注入的记忆能随消息到达模型。
    """
    prompt = system_prompt_with_date()
    if not msgs:
        return [ChatMessage("system", prompt)]
    first = msgs[0]
    first_role = first.get("role") if isinstance(first, dict) else getattr(first, "role", None)
    first_content = (first.get("content") if isinstance(first, dict)
                     else getattr(first, "content", "")) or ""
    rest = list(msgs[1:])
    # 原内容里有日期版没有的附加信息（长期记忆等）→ 保留拼接；纯 SYSTEM_CHAT
    # 占位（无附加信息）→ 直接替换，行为与旧版一致。
    extra = first_content.replace(SYSTEM_CHAT_PLAIN, "").strip()
    merged = prompt + (f"\n\n{extra}" if extra else "")
    if first_role == "system":
        if isinstance(first, dict):
            return [{"role": "system", "content": merged}] + rest
        return [ChatMessage("system", merged)] + rest
    return [ChatMessage("system", prompt)] + list(msgs)
