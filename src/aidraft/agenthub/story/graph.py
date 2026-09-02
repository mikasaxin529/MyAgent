"""剧本智能体图组装（三确认点跨轮状态机，与 yuwen 同构）。

管线：
  extract_brief（对话收创意）
  → gen_synopsis（梗概 → END，等确认【确认点1】）
  → confirm_synopsis（查盘：确认 → gen_characters / 修改 → END）
  → gen_characters（角色卡 → END，等确认【确认点2】）
  → confirm_characters（查盘：确认 → gen_portraits / 修改 → END）
  → gen_portraits（标准立绘生图，无 key 跳过不阻断）
  → gen_storyboard（分镜 → END，等确认【确认点3】）
  → confirm_storyboard（查盘：确认 → export / 修改 → END）
  → export（docx 剧本 + xlsx 分镜表 + HTML 预览 + 立绘图片包）
  → report（交付汇总）

跨轮状态机原理（与 yuwen 相同的心脏）：
  langgraph 每轮新实例 + 无 checkpointer，跨轮记忆走磁盘 state.json
  （outputs/story/<会话名>/state.json）。_route_after_brief 每轮重新
  求值且查盘，按 _stage_of（brief→synopsis→characters→storyboard→export）
  把本轮消息路由到当前确认点的节点：
    params_ready 且 stage=brief       → gen_synopsis
    stage=synopsis（未确认）           → confirm_synopsis
    stage=characters（未确认）         → confirm_characters
    stage=storyboard（未确认）         → confirm_storyboard
    stage=export（全确认后中断续跑）    → export
    params 未 ready 但像阶段应答       → 对应 confirm 兜底（查盘找回会话）
    其余                              → END（追问后等下一轮）

帧契约（前端接口，与 yuwen 帧并行不冲突）：
  {"type": "story_synopsis", "synopsis": {title, logline, themes, synopsis,
    acts:[{act,summary}], characters_brief:[{name,desc}], scene_count},
   "chips": [...]}
  {"type": "story_characters", "characters": {characters:[{id,name,role,
    description,ref_prompt,portrait?}]}, "chips": [...]}
  {"type": "story_storyboard", "storyboard": {scenes:[{scene_no,slug,synopsis,
    shots:[{id,shot_size,camera,subject,action,dialogue,sfx,image_prompt}]}]},
   "chips": [...]}
  其余（content/step/files/done）沿用通用帧。

模型绑定：agents.yaml 的 story_stage 键（provider:model，空走默认链），
gen_* 与 confirm_* 的改稿 LLM 共用一档——剧本管线单档足够。

首轮不上视觉审查（拍板项 2）：角色一致性靠双层锚点（description 文字
锚 + 立绘参照），VL 审查留给后续迭代。
"""
from __future__ import annotations

from typing import Any, Callable

from langgraph.graph import END, StateGraph

from .nodes import (
    _make_confirm_characters_node,
    _make_confirm_storyboard_node,
    _make_confirm_synopsis_node,
    _make_export_node,
    _make_extract_brief_node,
    _make_gen_characters_node,
    _make_gen_portraits_node,
    _make_gen_storyboard_node,
    _make_gen_synopsis_node,
    _make_report_node,
)
from .state import (
    StoryState,
    _find_pending_session,
    _load_state,
    _looks_like_stage_command,
    _stage_of,
)


def _model_kwargs_for(agent_key: str) -> dict:
    """从 agents.yaml 取该节点绑定的 provider/model（与 yuwen 同 loader）。"""
    try:
        from ...config import load_agent_models
        provider, model = load_agent_models().get(agent_key, ("", ""))
    except Exception:  # noqa: BLE001
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

def _route_after_brief(state: StoryState) -> str:
    """extract_brief 出口路由：查盘按确认点阶段分流。"""
    if not state.get("story_params_ready"):
        # 参数不齐但消息像对当前确认点的应答（chip 点击/确认词）→
        # 查盘找最近的待确认会话，路由到对应 confirm
        if _looks_like_stage_command(
                state.get("user_message") or state.get("task", "")):
            pending = _find_pending_session()
            if pending is not None:
                stage = _stage_of(pending[1])
                return {"synopsis": "confirm_synopsis",
                        "characters": "confirm_characters",
                        "storyboard": "confirm_storyboard"}.get(stage, "__end__")
        return "__end__"

    params = state.get("story_params", {})
    disk = _load_state(params)
    stage = _stage_of(disk)
    return {
        "brief": "gen_synopsis",
        "synopsis": "confirm_synopsis",
        "characters": "confirm_characters",
        "storyboard": "confirm_storyboard",
        "export": "export",
    }.get(stage, "gen_synopsis")


def _route_after_confirm_synopsis(state: StoryState) -> str:
    return "gen_characters" if state.get("story_synopsis_confirmed") else "__end__"


def _route_after_confirm_characters(state: StoryState) -> str:
    return "gen_portraits" if state.get("story_characters_confirmed") else "__end__"


def _route_after_confirm_storyboard(state: StoryState) -> str:
    return "export" if state.get("story_storyboard_confirmed") else "__end__"


# ---------------------------------------------------------------------------
# 图组装
# ---------------------------------------------------------------------------

def build_graph(
    gateway: Any,
    registry: Any,
    audit: Any | None = None,
    emitter: Callable[[dict], None] | None = None,
) -> Any:
    """组装并编译剧本智能体 langgraph 图。

    参数：
        gateway:  模型网关（gateway.chat）
        registry: Skill 注册中心（本图不使用）
        audit:    审计日志（可选）
        emitter:  事件回调，节点把帧推给 web 层
    """
    graph = StateGraph(StoryState)

    kw_stage = _model_kwargs_for("story_stage")

    graph.add_node("extract_brief", _make_extract_brief_node(gateway, emitter))
    graph.add_node("gen_synopsis",
                   _make_gen_synopsis_node(gateway, emitter, kw_stage))
    graph.add_node("confirm_synopsis",
                   _make_confirm_synopsis_node(gateway, emitter, kw_stage))
    graph.add_node("gen_characters",
                   _make_gen_characters_node(gateway, emitter, kw_stage))
    graph.add_node("confirm_characters",
                   _make_confirm_characters_node(gateway, emitter, kw_stage))
    graph.add_node("gen_portraits", _make_gen_portraits_node(emitter))
    graph.add_node("gen_storyboard",
                   _make_gen_storyboard_node(gateway, emitter, kw_stage))
    graph.add_node("confirm_storyboard",
                   _make_confirm_storyboard_node(gateway, emitter, kw_stage))
    graph.add_node("export", _make_export_node(emitter))
    graph.add_node("report", _make_report_node(emitter))

    graph.set_entry_point("extract_brief")

    graph.add_conditional_edges(
        "extract_brief",
        _route_after_brief,
        {
            "gen_synopsis": "gen_synopsis",
            "confirm_synopsis": "confirm_synopsis",
            "confirm_characters": "confirm_characters",
            "confirm_storyboard": "confirm_storyboard",
            "export": "export",
            "__end__": END,
        },
    )

    # 三个确认点节点都以 END 收尾（确认放行的走条件边继续）
    graph.add_edge("gen_synopsis", END)
    graph.add_conditional_edges(
        "confirm_synopsis", _route_after_confirm_synopsis,
        {"gen_characters": "gen_characters", "__end__": END})
    graph.add_edge("gen_characters", END)
    graph.add_conditional_edges(
        "confirm_characters", _route_after_confirm_characters,
        {"gen_portraits": "gen_portraits", "__end__": END})
    graph.add_edge("gen_portraits", "gen_storyboard")
    graph.add_edge("gen_storyboard", END)
    graph.add_conditional_edges(
        "confirm_storyboard", _route_after_confirm_storyboard,
        {"export": "export", "__end__": END})
    graph.add_edge("export", "report")
    graph.add_edge("report", END)

    return graph.compile()
