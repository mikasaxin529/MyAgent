"""confirm 节点：大纲确认 / 主题切换 / 配图偏好切换 / 自然语言改纲
（跨轮状态机第二环）。

图每轮新建实例，state 从零开始——大纲靠查盘（state.json）恢复。
意图判定用确定性关键词（确认词表 / 主题词表 / 配图词表），只有"改纲"才调 LLM：
确认与切主题走 LLM 是拿不确定性换零收益。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from ....gateway import ChatMessage
from ..prompts import SYSTEM_EDIT_OUTLINE, _themes_hint
from ..state import (
    YuwenState,
    _emit,
    _emit_outline,
    _find_pending_session,
    _load_state,
    _outline_summary,
    _parse_llm_json,
    _save_state,
    _step,
)
from ._page import _validate_outline
from ..theme_registry import match_theme, theme_display

# 确认意图词表：整句等于或前缀命中才算（"好"前缀避免"好像"误伤）
_EXACT_CONFIRM = {"确认", "确认大纲", "确认大纲，开始生成", "可以", "可以的",
                  "没问题", "开始生成", "直接生成", "就这样", "同意", "好的",
                  "好", "行", "ok", "OK", "Ok", "继续", "生成吧", "开始吧"}
_PREFIX_CONFIRM = ("确认", "可以", "好的", "没问题", "开始生成", "直接生成",
                   "同意", "ok", "OK")
# 配图偏好切换（确定性）：须先含触发词，再匹配风格/数量词表（顺序即优先级）
_IMAGE_TRIGGERS = ("配图", "插图", "生图")
_IMAGE_STYLE_MAP = [
    (("水彩",), "水彩"),
    (("剪纸",), "剪纸"),
    (("国风", "水墨"), "国风"),
    (("卡通",), "卡通"),
    (("绘本",), "绘本"),
]
_IMAGE_COUNT_MAP = [
    # none 在前："不要/不配"类否定词优先于"多/全部"类（"不要全部配图"→none）
    (("不要", "不用", "不配", "无需", "不需要", "去掉"), "none"),
    (("全部", "每张", "每页都", "都配", "多一些", "多一点", "多配"), "all"),
    (("最少", "少一些", "少一点", "少配", "精简", "省钱"), "minimal"),
]
_COUNT_LABELS = {"minimal": "最少配图", "all": "全部配图", "none": "不配图"}


def _detect_theme(text: str) -> str | None:
    """从用户消息解析主题切换意图，返回目标主题或 None。

    匹配走 theme_registry（keywords 按词长降序，"青绿"先于"绿"不误捕），
    词表随 themes/*.json 即插即用。
    """
    return match_theme(text)


def _detect_confirm(text: str) -> bool:
    """从用户消息解析确认意图（确定性规则，不靠 LLM）。"""
    s = (text or "").strip()
    if not s:
        return False
    if s in _EXACT_CONFIRM:
        return True
    return any(s.startswith(w) for w in _PREFIX_CONFIRM)


def _detect_image_prefs(text: str) -> dict:
    """从用户消息解析配图偏好切换（确定性规则，不靠 LLM）。

    必须先含触发词（配图/插图/生图）再匹配风格/数量词表——"这课讲的是
    水彩画"这类无触发词的消息不误伤。返回命中的键（可为空 dict）。
    """
    s = text or ""
    if not any(t in s for t in _IMAGE_TRIGGERS):
        return {}
    prefs: dict = {}
    for keywords, style in _IMAGE_STYLE_MAP:
        if any(k in s for k in keywords):
            prefs["image_style"] = style
            break
    for keywords, count in _IMAGE_COUNT_MAP:
        if any(k in s for k in keywords):
            prefs["image_count"] = count
            break
    return prefs


def _make_confirm_node(gateway: Any, emitter: Callable[[dict], None] | None,
                       model_kwargs: dict | None = None):
    """confirm 节点工厂：查盘恢复大纲 → 确认放行 / 切主题 / LLM 改纲。"""
    model_kwargs = model_kwargs or {}

    async def confirm(state: YuwenState) -> dict:
        _step(emitter, "confirm", "大纲确认", "running")

        visited = list(state.get("nodes_visited") or [])
        if "confirm" not in visited:
            visited.append("confirm")

        params = dict(state.get("yuwen_params") or {})
        user_msg = state.get("user_message") or state.get("task", "")

        # 查盘恢复大纲。params 齐全时按 session 精确定位；params 缺失
        # （用户点了大纲 chip"确认大纲，开始生成"，被 extract_params 判为
        # 无有效课文名 → 兜底路由进 confirm）时，扫盘找回最近的待确认会话。
        disk = _load_state(params) if params.get("title") else {}
        outline = disk.get("yuwen_outline") or {}
        if not outline.get("pages"):
            pending = _find_pending_session(
                session_short=str(state.get("session_id") or "")[-8:])
            if pending:
                params, disk = pending
                outline = disk.get("yuwen_outline") or {}
        if not outline.get("pages"):
            _step(emitter, "confirm", "大纲确认", "done", "盘上无大纲")
            return {"yuwen_outline_confirmed": False, "nodes_visited": visited}

        # 主题切换（确定性）：就地改 outline.meta.theme，随后与确认/改纲组合
        theme = _detect_theme(user_msg)
        if theme:
            outline.setdefault("meta", {})["theme"] = theme

        # 配图偏好切换（确定性）：更新 image_style/image_count 进 params，
        # gen_images 读 yuwen_params 生效；与主题一样可与确认组合
        img_prefs = _detect_image_prefs(user_msg)
        if img_prefs:
            params.update(img_prefs)

        already_confirmed = bool(disk.get("yuwen_outline_confirmed"))
        # 含主题/配图切换的句子放宽确认判定："换成墨绿主题，确认"——确认词
        # 不必在句首。纯修改指令不含确认词表，不会误放行。
        combo_confirm = (bool(theme) or bool(img_prefs)) and any(
            w in user_msg for w in _PREFIX_CONFIRM)
        is_confirm = (already_confirmed or _detect_confirm(user_msg)
                      or combo_confirm)

        def _prefs_desc() -> str:
            bits = []
            if theme:
                bits.append(f"主题 {theme_display(theme)}")
            if img_prefs.get("image_style"):
                bits.append(f"配图风格 {img_prefs['image_style']}")
            if img_prefs.get("image_count"):
                bits.append(_COUNT_LABELS[img_prefs["image_count"]])
            return "，".join(bits)

        # 路径 A：确认（可含主题/配图切换）→ 放行 gen_slides
        # yuwen_params 回传：params 可能经 _find_pending_session 从盘上找回
        # （chip 点击轮 extract 判空），下游 gen_slides/render 定位 session
        # 目录依赖它，不回传会把产物写进 "-unknown" 目录。
        if is_confirm:
            _save_state(params, yuwen_outline=outline,
                        yuwen_params=params,
                        yuwen_outline_confirmed=True)
            desc = _prefs_desc()
            detail = f"大纲已确认（{desc}）" if desc else "大纲已确认"
            _step(emitter, "confirm", "大纲确认", "done", detail)
            return {"yuwen_params": params,
                    "yuwen_outline": outline,
                    "yuwen_outline_confirmed": True,
                    "nodes_visited": visited}

        # 路径 B：纯主题切换 → 落盘重发帧，等下一轮确认
        if theme:
            _save_state(params, yuwen_outline=outline, yuwen_params=params)
            _emit_outline(emitter, outline)
            disp = theme_display(theme)
            _step(emitter, "confirm", "大纲确认", "done", f"主题已切换为 {disp}")
            return {"yuwen_params": params,
                    "yuwen_outline": outline,
                    "yuwen_outline_confirmed": False,
                    "final_answer": f"主题已切换为 {disp}，回复\"确认\"开始生成。",
                    "nodes_visited": visited}

        # 路径 B2：纯配图偏好切换 → 落盘提示，等下一轮确认
        if img_prefs:
            _save_state(params, yuwen_outline=outline, yuwen_params=params)
            desc = _prefs_desc()
            answer = f"配图偏好已更新（{desc}），回复\"确认\"开始生成。"
            _emit(emitter, {"type": "content", "delta": answer,
                            "step_id": "confirm"})
            _step(emitter, "confirm", "大纲确认", "done", f"配图偏好已更新（{desc}）")
            return {"yuwen_params": params,
                    "yuwen_outline": outline,
                    "yuwen_outline_confirmed": False,
                    "final_answer": answer,
                    "nodes_visited": visited}

        # 路径 C：其他自然语言 → LLM 单次改纲（指令应用到 outline JSON）
        system_prompt = SYSTEM_EDIT_OUTLINE.format(
            outline_json=json.dumps(outline, ensure_ascii=False, indent=1),
            themes=_themes_hint())
        edited: dict | None = None
        err = ""
        try:
            resp = gateway.chat(
                [ChatMessage("system", system_prompt),
                 ChatMessage("user", user_msg)],
                temperature=0.3, json_mode=True, **model_kwargs)
            parsed = _parse_llm_json(resp.content)
            problems = _validate_outline(parsed)
            if problems:
                err = "；".join(problems[:3])
            else:
                edited = parsed
        except Exception as exc:  # noqa: BLE001 - 改纲失败降级为提示
            err = str(exc)

        if edited is None:
            # 改纲失败：重发当前大纲 + 提示，不阻断（用户可再描述或直接确认）
            _emit_outline(emitter, outline)
            answer = (f"大纲修改指令未能生效（{err[:80]}），已保留原大纲。"
                      f"可重新描述修改点，或回复\"确认\"直接生成。")
            _emit(emitter, {"type": "content", "delta": answer,
                            "step_id": "confirm"})
            _step(emitter, "confirm", "大纲确认", "done", "改纲失败，保留原稿")
            return {"yuwen_params": params,
                    "yuwen_outline": outline,
                    "yuwen_outline_confirmed": False,
                    "final_answer": answer,
                    "nodes_visited": visited}

        _save_state(params, yuwen_outline=edited, yuwen_params=params)
        _emit_outline(emitter, edited)
        answer = _outline_summary(edited) + " 如无其他修改，回复\"确认\"开始生成。"
        _step(emitter, "confirm", "大纲确认", "done", "大纲已按指令修改")
        return {"yuwen_params": params,
                "yuwen_outline": edited,
                "yuwen_outline_confirmed": False,
                "yuwen_error": "",
                "final_answer": answer,
                "nodes_visited": visited}

    return confirm
