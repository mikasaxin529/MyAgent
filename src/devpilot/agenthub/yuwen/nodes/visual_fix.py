"""visual_fix 节点：视觉审查修复闭环（≤1 轮，降分回滚，全程可降级）。

visual_review 只报告不修复——本节点把 medium/high 级、内容层可修的版面
问题转成"单页 LLM 重生成"（照 revise.py 模式：带问题清单重生成该页、
schema 校验失败保留原页、回写 doc + tmp_content.json），再经 render →
visual_review 复查对比分数：升/平保留新版，降则回滚备份并再渲染一次
交付原版（visual_review 见 rollback 标记跳过复查，省一次 VLM 钱）。

节点按 state 标记分两职（langgraph 增量合并，同一节点两次进入）：
- 无 pending 标记 → 修复阶段：挑页备份、重生成、置 pending=1（出口走 render）
- pending=True   → 对比阶段：读复查 score 决定保留或回滚（回滚置 rollback=1，
                   出口仍走 render；保留则直进 report）

成本护栏：只修 severity=high、或 medium 且抽查页数 ≤ _MEDIUM_MAX_PAGES 的
issue；low 只统计；color_mismatch / theme_mismatch / other 属渲染/主题层
问题，重生成内容无意义不修；单轮重生成页数上限 _MAX_FIX_PAGES（按严重度
排序取前 3 页）；全流程最多 1 轮（yuwen_visual_fix_rounds）。

降级原则（贯穿全管线）：LLM 失败 / 校验失败 / 写盘失败 / 一切异常 →
保留原版放行 report，step 帧注明原因，绝不 raise。
"""
from __future__ import annotations

import copy
import json
from typing import Any, Callable

from ....gateway import ChatMessage
from ..prompts import SYSTEM_VISUAL_FIX, _read_ref
from ..state import (
    YuwenState,
    _content_path,
    _emit,
    _parse_llm_json,
    _step,
)
from ._page import _call_llm, _validate_page_slide

# 内容层可修类型（重生成该页有意义）；其余归渲染/主题层，只统计不修
_FIXABLE_TYPES = {"title_unclear", "text_overlap", "image_cropped",
                  "image_text_overlap", "text_too_small",
                  "too_much_whitespace"}

# issue.type 中文释义（喂进提示词，让 LLM 知道审查者看到了什么）
_TYPE_LABELS = {
    "title_unclear": "标题不清晰",
    "text_overlap": "文字遮挡",
    "image_cropped": "图片被裁切",
    "image_text_overlap": "图片与文字重叠",
    "text_too_small": "字体过小",
    "too_much_whitespace": "留白过多",
    "color_mismatch": "配色不当",
    "theme_mismatch": "图片与主题不符",
    "other": "其他版面问题",
}

# ---- 成本护栏参数（汇报口径，测试断言与此一致）----
_MAX_FIX_PAGES = 3      # 单轮 LLM 重生成页数上限
_MEDIUM_MAX_PAGES = 4   # 抽查 ≤4 页（小 deck）时 medium 才值得修
_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _actionable_issues(visual: dict) -> list[dict]:
    """从 visual 帧挑出值得重生成的页：[{page_id, issues:[...]}]。

    过滤规则见模块 docstring（护栏参数）。同页多 issue 合并；页间按
    组内最高 severity 排序（并列按 page_id 保证确定性），截前 3 页。
    路由函数（graph._route_after_visual）以本函数结果为唯一判据，
    避免路由与节点两套挑选逻辑漂移。
    """
    if not visual.get("available"):
        return []
    n_sampled = len(visual.get("pages") or [])
    by_page: dict[str, list[dict]] = {}
    for it in visual.get("issues") or []:
        page_id = str(it.get("page_id") or "")
        typ = str(it.get("type") or "")
        sev = str(it.get("severity") or "low")
        if not page_id or typ not in _FIXABLE_TYPES:
            continue
        # low 只统计不修；medium 仅小 deck 触发
        if sev == "high" or (sev == "medium" and n_sampled <= _MEDIUM_MAX_PAGES):
            by_page.setdefault(page_id, []).append(it)
    if not by_page:
        return []
    ranked = sorted(
        by_page.items(),
        key=lambda kv: (min(_SEVERITY_ORDER.get(str(i.get("severity")), 2)
                            for i in kv[1]), kv[0]))
    return [{"page_id": pid, "issues": items} for pid, items in
            ranked[:_MAX_FIX_PAGES]]


def _rewrite_content(params: dict, doc: dict) -> str:
    """doc 重写 tmp_content.json（render 读盘），返回落盘路径；失败返回 ''。"""
    try:
        tmp_path = _content_path(params)
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        return str(tmp_path)
    except Exception:  # noqa: BLE001 - 写盘失败不阻断，render 用旧盘
        return ""


def _make_visual_fix_node(gateway: Any, emitter: Callable[[dict], None] | None,
                          model_kwargs: dict | None = None):
    """visual_fix 节点工厂：修复阶段 + 对比/回滚阶段两职一体。"""
    model_kwargs = model_kwargs or {}

    async def visual_fix(state: YuwenState) -> dict:
        visited = list(state.get("nodes_visited") or [])
        if "visual_fix" not in visited:
            visited.append("visual_fix")
        try:
            if state.get("yuwen_visual_fix_pending"):
                return await _compare(state, visited)
            return await _fix(state, visited)
        except Exception as exc:  # noqa: BLE001 - 一切异常保留原版放行 report
            note = f"视觉修复异常（{str(exc)[:60]}），保留原版"
            _step(emitter, "visual_fix", "视觉修复", "done", note)
            return {"yuwen_visual_fix_pending": False,
                    "yuwen_visual_fix_note": note,
                    "nodes_visited": visited}

    async def _fix(state: YuwenState, visited: list) -> dict:
        """修复阶段：挑页备份 → 逐页 LLM 重生成 → 回写 doc + 盘，置 pending。"""
        doc = state.get("yuwen_content") or {}
        visual = state.get("yuwen_visual") or {}
        params = state.get("yuwen_params", {})
        targets = _actionable_issues(visual)
        _step(emitter, "visual_fix", "视觉修复", "running",
              f"{len(targets)} 页待修复")
        if not targets:
            # 路由已挡（无可修 issue 不进本节点）——防御性放行
            note = "无可自动修复的版面问题"
            _step(emitter, "visual_fix", "视觉修复", "done", note)
            return {"yuwen_visual_fix_rounds": 1, "yuwen_visual_fix_note": note,
                    "nodes_visited": visited}

        slides = doc.get("slides") or []
        meta = doc.get("meta") or {}
        by_id = {str(s.get("id", "")): s for s in slides}
        backup = copy.deepcopy(doc)  # 降分时回滚的基线（深拷贝防原对象被改）
        fixed = 0
        for target in targets:
            page_id = target["page_id"]
            slide = by_id.get(page_id)
            if slide is None:
                continue  # 幽灵页 ID：跳过
            problems = "\n".join(
                f"- [{_TYPE_LABELS.get(str(i.get('type')), '其他版面问题')}] "
                f"{i.get('suggestion') or '请改善该处版面'}"
                for i in target["issues"])
            system_prompt = SYSTEM_VISUAL_FIX.format(
                schema=_read_ref("schema.md"),
                slide_json=json.dumps(slide, ensure_ascii=False, indent=1),
                problems=problems)
            revised: dict | None = None
            try:
                resp = _call_llm(
                    gateway, "chat",
                    [ChatMessage("system", system_prompt),
                     ChatMessage("user", "请输出修复后的完整单页 JSON。")],
                    model_kwargs, temperature=0.4, json_mode=True)
                parsed = _parse_llm_json(resp.content)
                revised = _validate_page_slide(parsed, meta)
            except Exception:  # noqa: BLE001 - 修复失败保留原页（改坏不如不改）
                revised = None
            if revised is not None:
                # 锁身份三键：提示词已要求不变，代码层再兜底（防栏目漂移）
                revised["id"] = slide.get("id", revised.get("id"))
                revised["kind"] = slide.get("kind", revised.get("kind"))
                revised["title"] = slide.get("title", revised.get("title"))
                idx = slides.index(slide)
                slides[idx] = revised
                by_id[page_id] = revised
                fixed += 1
                _emit(emitter, {"type": "content",
                                "delta": f"已按视觉审查重生成 {page_id}\n",
                                "step_id": "visual_fix"})

        rounds = int(state.get("yuwen_visual_fix_rounds") or 0) + 1
        if fixed == 0:
            note = f"{len(targets)} 页视觉修复均未产出有效页面，保留原版"
            _step(emitter, "visual_fix", "视觉修复", "done", note)
            return {"yuwen_visual_fix_rounds": rounds,
                    "yuwen_visual_fix_pending": False,
                    "yuwen_visual_fix_note": note,
                    "nodes_visited": visited}

        doc["slides"] = slides
        path = _rewrite_content(params, doc)
        note = f"已重生成 {fixed}/{len(targets)} 页，重新渲染复查中"
        _step(emitter, "visual_fix", "视觉修复", "done", note)
        out: dict = {
            "yuwen_content": doc,
            "yuwen_visual_fix_rounds": rounds,
            "yuwen_visual_fix_pending": True,
            "yuwen_visual_fix_rollback": False,
            "yuwen_visual_fix_backup": backup,
            "yuwen_visual_fix_prev_score": int(visual.get("score") or 0),
            "yuwen_visual_fix_prev_visual": copy.deepcopy(visual),
            "yuwen_visual_fix_note": note,
            "nodes_visited": visited,
        }
        if path:
            out["yuwen_content_path"] = path
        return out

    async def _compare(state: YuwenState, visited: list) -> dict:
        """对比阶段（复查后二次进入）：升/平保留，降分回滚备份再渲染。"""
        _step(emitter, "visual_fix", "视觉修复复查", "running")
        new_visual = state.get("yuwen_visual") or {}
        prev_score = int(state.get("yuwen_visual_fix_prev_score") or 0)
        base = {"yuwen_visual_fix_pending": False, "nodes_visited": visited}

        if not new_visual.get("available"):
            # 复查失败无从对比——只接受"有证据不降分"的修复（质量棘轮），
            # 走回滚路径交付原版（原版经 V1 审查验证过，修后版未见新截图）
            return await _rollback(state, visited, base,
                                   prev_score, prev_score,
                                   reason=f"（{new_visual.get('reason') or '复查未完成'}）")

        new_score = int(new_visual.get("score") or 0)
        if new_score >= prev_score:
            note = f"视觉修复有效：{prev_score} → {new_score} 分，保留修复版"
            _step(emitter, "visual_fix", "视觉修复复查", "done", note)
            return {**base, "yuwen_visual_fix_rollback": False,
                    "yuwen_visual_fix_note": note}

        # 分数下降 → 回滚（rollback 标记让后续 render 重渲染原版、
        # visual_review 跳过复查透传修复前结果，省一次 VLM 钱）
        return await _rollback(state, visited, base, prev_score, new_score)

    async def _rollback(state: YuwenState, visited: list, base: dict,
                        prev_score: int, new_score: int,
                        reason: str = "") -> dict:
        """回滚共用出口：备份 doc 重写盘 + 置 rollback（备份缺失则保留现状）。"""
        backup = state.get("yuwen_visual_fix_backup") or {}
        params = state.get("yuwen_params", {})
        out: dict = {**base, "yuwen_visual_fix_rollback": True}
        cmp = f"视觉修复未提升（{prev_score} → {new_score} 分{reason}）"
        if backup.get("slides"):
            path = _rewrite_content(params, backup)
            out["yuwen_content"] = backup
            if path:
                out["yuwen_content_path"] = path
            note = f"{cmp}，已回滚原版"
        else:
            # 备份缺失（异常状态）：无法回滚，只能保留现状
            out["yuwen_visual_fix_rollback"] = False
            note = f"{cmp}，但备份缺失，保留现状"
        _step(emitter, "visual_fix", "视觉修复复查", "done", note)
        return {**out, "yuwen_visual_fix_note": note}

    return visual_fix
