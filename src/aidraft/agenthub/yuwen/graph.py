"""语文智能体图组装（阶段 2a：多阶段管线）。

管线：
  extract_params（对话收参数）
  → research（联网搜教学设计 + 课文原文，可选增强：无 TAVILY_API_KEY /
    搜索失败均降级放行不阻断；结果落盘 TTL 7 天，改纲轮不重搜）
  → gen_outline（生成大纲 → END，等用户确认）
  → confirm（查盘恢复大纲：确认放行 / 切主题 / 改纲 → END 或继续）
  → gen_slides（逐页生成 + 页级反思重试）
  → gen_plan（教案 + 学习单）
  → review（AI 审查评分）⇄ revise（按问题清单修订，≤2 轮）
  → gen_images（AI 配图回填，无 key 跳过）
  → render（subprocess 三件套）
  → visual_review（PPTX 转逐页图 → 百炼 qwen-vl 视觉审查，无 key/无
    LibreOffice 降级跳过，不阻断）
  → visual_fix（视觉修复闭环，≤1 轮）：medium/high 且内容层可修的版面
    问题 → 单页 LLM 重生成 → render 重渲染 → visual_review 复查；
    分数不降保留新版，降分回滚备份再渲染原版（回滚后 visual_review 经
    rollback 标记跳过复查，不再花 VLM 钱）。无可修问题/降级直接 report
  → report（交付汇总）

跨轮状态机原理（本图的心脏）：
  langgraph 每轮请求都 build_graph 新实例 + astream 全新 state——图内没有
  任何检查点。跨轮记忆全部走磁盘 state.json（session 目录下，_OUTPUTS_DIR/
  yuwen/<会话名>/state.json，由 params 派生会话名，天然与 render/report 同键）。
  关键在 extract_params 之后的**条件路由函数 _route_after_params 每轮重新
  求值且查盘**：同一条边在不同轮根据"盘上有没有大纲 / 大纲确认了没有"
  走不同分支——
    params_ready 且盘上无 outline        → research → gen_outline（首轮，
                                          搜索降级时 research 直通）
    params_ready 且盘上有 outline 未确认 → confirm（用户回复确认/改纲）
    params_ready 且盘上 outline 已确认   → gen_slides（罕见：确认后中断续跑）
    params 未 ready 但消息像大纲指令     → confirm（chip 点击/确认词被参数
                                          提取误判时兜底，_find_pending_session
                                          从盘上找回会话）
    其余                                → END（追问后等下一轮）
  gen_outline / confirm（未确认路径）都以 END 收尾——本轮到此为止，用户下一轮
  的消息重新进图，路由查盘把流程接上。这就是"无状态图 + 有状态盘"实现的
  多轮人机协同管线。

帧契约（与前端 2c 的接口，其余帧类型沿用阶段 1）：
  {"type": "outline", "outline": {pages:[{id,kind,title,points,period}], meta:{...}},
   "chips": ["确认大纲，开始生成", "第1页改成…", "换青蓝主题", …],
   "options": {"themes": [{name, display, swatch, tags}]}}  # M1：注册表全集，
                                                            # 前端不再维护静态映射
  {"type": "review", "review": {"scores": {structure,pedagogy,content,stage_fit},
                                "issues": [{page_id, problems:[str]}], "pass": bool}}
  {"type": "visual", "visual": {available: bool,   // false=未跑（无key/无soffice/失败）
                                reason: str,       // available=false 时的原因
                                score: int,        // 各抽查页平均分 0-100
                                pages: [{page_id, score, image: "/files/yuwen/<session>/review/sNN.png"}],
                                issues: [{page_id, type, severity: low|medium|high,
                                          bbox: [x1,y1,x2,y2](0-1000归一化), suggestion}]}}
  注意：visual_fix 闭环内 visual 帧可能发多次（修复后复查一次；回滚后
  visual_review 经 rollback 标记透传修复前结果，不重调 VLM）——前端按
  "最新帧覆盖"渲染即可，无新增帧类型；visual_fix 进度走通用 step 帧
  （id=visual_fix, label=视觉修复/视觉修复复查）。
  meta.theme 值域由 theme_registry 扫描 themes/*.json 派生（即插即用），
  渲染器由 renderer agent 消费。
  gen_images 回写的 image.src 是**相对 session 目录**路径（如 "assets/s03_2.png"），
  渲染器需解析为绝对路径。

模型绑定：config/agents.yaml 的 yuwen_outline / yuwen_slide / yuwen_review
三个键（"provider:model" 或空串走默认链）。分节点消费方：
  yuwen_outline → gen_outline + confirm（改纲 LLM）
  yuwen_slide   → gen_slides + revise + visual_fix（问题页重生成）
  yuwen_review  → review + gen_plan
注意：Gateway.chat 目前不支持 provider 参数（只有 stream_chat 支持），
nodes/_page._call_llm 按签名探测传参，chat 绑定暂静默降级默认链。
"""
from __future__ import annotations

from typing import Any, Callable

from langgraph.graph import END, StateGraph

from .nodes import (
    _make_confirm_node,
    _make_extract_params_node,
    _make_gen_images_node,
    _make_gen_outline_node,
    _make_gen_plan_node,
    _make_gen_slides_node,
    _make_render_node,
    _make_report_node,
    _make_research_node,
    _make_review_node,
    _make_revise_node,
    _make_visual_fix_node,
    _make_visual_review_node,
)
from .nodes.visual_fix import _actionable_issues
from .state import (
    YuwenState,
    _find_pending_session,
    _load_state,
    _looks_like_outline_command,
)


# ---------------------------------------------------------------------------
# 模型绑定读取
# ---------------------------------------------------------------------------

def _model_kwargs_for(agent_key: str) -> dict:
    """从 agents.yaml 取该节点绑定的 provider/model。

    复用 aidraft.config.load_agent_models（与 general 图同一 loader，
    yaml 缺失/解析失败优雅降级）。绑定为空串或解析不出 provider → 返回
    {}（不传参走网关默认主备链）。
    """
    try:
        from ...config import load_agent_models
        provider, model = load_agent_models().get(agent_key, ("", ""))
    except Exception:  # noqa: BLE001 - 配置读取失败不阻断建图
        return {}
    kw: dict = {}
    if provider:
        kw["provider"] = provider
    if model:
        kw["model"] = model
    return kw


# ---------------------------------------------------------------------------
# 条件边：跨轮状态机路由
# ---------------------------------------------------------------------------

def _route_after_params(state: YuwenState) -> str:
    """extract_params 出口路由：查盘决定本轮该走大纲生成、确认还是 END。

    这是跨轮状态机的求值点——每轮新图实例跑这条边，盘上状态变了分支就变。
    """
    if not state.get("yuwen_params_ready"):
        # 参数不齐：但用户可能点的是大纲 chip（"确认大纲，开始生成"）或
        # 主题切换词——extract_params 的 LLM 抽不出课文名，params_ready
        # 为 False。此时若盘上有待确认大纲，兜底进 confirm。
        if _looks_like_outline_command(
                state.get("user_message") or state.get("task", "")):
            pending = _find_pending_session()
            if pending is not None:
                return "confirm"
        return "__end__"

    params = state.get("yuwen_params", {})
    disk = _load_state(params)
    outline = disk.get("yuwen_outline") or {}
    if not outline.get("pages"):
        return "research"
    if disk.get("yuwen_outline_confirmed"):
        # 已确认（上一轮 confirm 放行但生成中断/或确认后用户又发消息）：
        # 直接续跑逐页生成。confirm 节点里 already_confirmed 也放行，双保险。
        return "gen_slides"
    return "confirm"


def _route_after_confirm(state: YuwenState) -> str:
    """confirm 出口：确认放行 → gen_slides；未确认（改纲/切主题）→ END 等下轮。"""
    if state.get("yuwen_outline_confirmed"):
        return "gen_slides"
    return "__end__"


def _route_after_review(state: YuwenState) -> str:
    """review 出口：pass → 配图；不 pass 且修订 <2 轮 → revise；轮数耗尽 → 放行。

    审查是提质不是阻断——2 轮修订仍不过就带着问题继续渲染，
    report 里注明评分，用户可手动改产物。
    """
    review = state.get("yuwen_review") or {}
    if review.get("pass"):
        return "gen_images"
    if int(state.get("yuwen_revise_rounds") or 0) < 2:
        return "revise"
    return "gen_images"


def _route_after_visual(state: YuwenState) -> str:
    """visual_review 出口路由（视觉修复闭环的总闸）。

    - pending=True：修复已做完、刚复查回来 → 进 visual_fix 对比分数
    - rounds≥1：闭环最多 1 轮 → report（复查对比除外，见上）
    - 审查降级 / 无内容层可修 issue（含只有 low、只有渲染层类型）→ report
    - 其余 → visual_fix 执行修复
    判据用 _actionable_issues（与节点同一套挑选逻辑，不漂移）。
    """
    if state.get("yuwen_visual_fix_pending"):
        return "visual_fix"
    if int(state.get("yuwen_visual_fix_rounds") or 0) >= 1:
        return "report"
    visual = state.get("yuwen_visual") or {}
    if not _actionable_issues(visual):
        return "report"
    return "visual_fix"


def _route_after_fix(state: YuwenState) -> str:
    """visual_fix 出口路由：修了待复查 → render；回滚了待重渲染 → render；
    没修成 / 对比后保留新版 → report。"""
    if state.get("yuwen_visual_fix_pending") or state.get("yuwen_visual_fix_rollback"):
        return "render"
    return "report"


# ---------------------------------------------------------------------------
# 图组装
# ---------------------------------------------------------------------------

def build_graph(
    gateway: Any,
    registry: Any,
    audit: Any | None = None,
    emitter: Callable[[dict], None] | None = None,
) -> Any:
    """组装并编译语文智能体 langgraph 图。

    参数：
        gateway:  模型网关（gateway.chat / gateway.stream_chat）
        registry: Skill 注册中心（本图不使用）
        audit:    审计日志（可选）
        emitter:  事件回调，节点把帧推给 web 层

    返回：
        langgraph 编译后的图，可 .astream(input) 异步流式执行。
    """
    graph = StateGraph(YuwenState)

    # 分节点模型绑定（yaml 每次建图重读，改配置即时生效）
    kw_outline = _model_kwargs_for("yuwen_outline")
    kw_slide = _model_kwargs_for("yuwen_slide")
    kw_review = _model_kwargs_for("yuwen_review")

    # 注册节点
    graph.add_node("extract_params", _make_extract_params_node(gateway, emitter))
    graph.add_node("research", _make_research_node(emitter))
    graph.add_node("gen_outline", _make_gen_outline_node(gateway, emitter, kw_outline))
    graph.add_node("confirm", _make_confirm_node(gateway, emitter, kw_outline))
    graph.add_node("gen_slides", _make_gen_slides_node(gateway, emitter, kw_slide))
    graph.add_node("gen_plan", _make_gen_plan_node(gateway, emitter, kw_review))
    graph.add_node("review", _make_review_node(gateway, emitter, kw_review))
    graph.add_node("revise", _make_revise_node(gateway, emitter, kw_slide))
    graph.add_node("gen_images", _make_gen_images_node(emitter))
    graph.add_node("render", _make_render_node(emitter))
    graph.add_node("visual_review", _make_visual_review_node(emitter))
    graph.add_node("visual_fix", _make_visual_fix_node(gateway, emitter, kw_slide))
    graph.add_node("report", _make_report_node(emitter))

    # 入口
    graph.set_entry_point("extract_params")

    # extract_params →（跨轮路由）→ research / confirm / gen_slides / END
    graph.add_conditional_edges(
        "extract_params",
        _route_after_params,
        {
            "research": "research",
            "confirm": "confirm",
            "gen_slides": "gen_slides",
            "__end__": END,
        },
    )

    # research → gen_outline：搜资料（无 key / 失败均降级放行，不阻断）
    graph.add_edge("research", "gen_outline")

    # 大纲 → END（本轮结束，等用户确认）
    graph.add_edge("gen_outline", END)

    # confirm → 确认放行继续生成 / 未确认 END 等下一轮
    graph.add_conditional_edges(
        "confirm",
        _route_after_confirm,
        {"gen_slides": "gen_slides", "__end__": END},
    )

    # 主链：逐页生成 → 教案 → 审查 ⇄ 修订 → 配图 → 渲染 → 视觉审查 → 报告
    graph.add_edge("gen_slides", "gen_plan")
    graph.add_edge("gen_plan", "review")
    graph.add_conditional_edges(
        "review",
        _route_after_review,
        {"revise": "revise", "gen_images": "gen_images"},
    )
    graph.add_edge("revise", "review")  # 修订后再评一轮
    graph.add_edge("gen_images", "render")
    graph.add_edge("render", "visual_review")
    # 视觉修复闭环：visual_review →（有可修问题且未修过）→ visual_fix →
    # render（重渲染复查 / 回滚重渲染）→ visual_review（rollback 时透传
    # 跳过）→ report。轮次闸门在 _route_after_visual（rounds≥1 放行）。
    graph.add_conditional_edges(
        "visual_review",
        _route_after_visual,
        {"visual_fix": "visual_fix", "report": "report"},
    )
    graph.add_conditional_edges(
        "visual_fix",
        _route_after_fix,
        {"render": "render", "report": "report"},
    )
    graph.add_edge("report", END)

    return graph.compile()
