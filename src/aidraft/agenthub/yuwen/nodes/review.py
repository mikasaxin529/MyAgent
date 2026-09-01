"""review 节点：AI 质量审查（结构层程序预检 + 内容层 LLM 抽查评分）。

评分维度：structure/pedagogy/content/stage_fit 各 1-5 分。
pass 规则：LLM 判定（无 issues 且均分 ≥4）。发 review 帧供前端评分卡展示。
抽查页用 random.seed(42) 固定——测试可重现，且同一 doc 重评结果稳定。
"""
from __future__ import annotations

import json
import random
from typing import Any, Callable

from ....gateway import ChatMessage
from ..prompts import SYSTEM_REVIEW
from ..state import (
    YuwenState,
    _emit,
    _parse_llm_json,
    _step,
)
from ._page import _call_llm

# 低段每页元素数上限（学段密度约束，结构层预检用）
_MAX_ELEMS = {"低段": 4, "中段": 5, "高段": 6}

# 版式专用栏目：toc 固定 2 元素、challenge/scene-strip 常为 1 元素——
# 元素少是这类页的正常形态，结构预检单列标注，避免 LLM 按密度误判扣分。
_FORMATTED_KINDS = {"toc", "challenge", "scene-strip"}

# 每课时页数对标指引上限：超过即明显偏多（收敛版：每课时 10-14 页）
_PAGE_TARGET_MAX = 16


def _structure_report(doc: dict) -> str:
    """结构层预检：程序统计（页数/kind 序列/period 分布/密度/空元素），
    喂给 LLM 核对——数值统计 LLM 做不好也不该做，它只管判断。"""
    slides = doc.get("slides", [])
    meta = doc.get("meta", {})
    stage = meta.get("stage", "")
    periods = meta.get("periods", 1)
    lines = [f"- 共 {len(slides)} 页，{periods} 课时，学段 {stage or '未知'}",
             "- 对标指引：每课时 10-14 页（低段取上限），单页宁精不滥"]

    # kind 序列
    kinds = [s.get("kind", "") for s in slides]
    lines.append(f"- kind 序列：{' > '.join(kinds[:30])}{' …' if len(kinds) > 30 else ''}")

    # period 分布
    dist: dict[int, int] = {}
    for s in slides:
        p = s.get("period", 1)
        dist[p] = dist.get(p, 0) + 1
    lines.append(f"- 课时分布：{dict(sorted(dist.items()))}")
    if int(periods) >= 2 and len(dist) < 2:
        lines.append("  ⚠ 多课时课件但页面 period 未分出两个课时")
    for p in sorted(dist):
        if dist[p] > _PAGE_TARGET_MAX:
            lines.append(f"  ⚠ 第 {p} 课时 {dist[p]} 页，明显超每课时 10-14 页指引，建议合并同类页")

    # 密度与空元素
    max_n = _MAX_ELEMS.get(stage, 6)
    density_bad = []
    empty_pages = []
    formatted_pages = []
    for s in slides:
        elems = s.get("elements", [])
        if s.get("kind") in _FORMATTED_KINDS and elems:
            # 版式栏目页：1-2 个元素是设计形态（目录左图右列/闯关单卡/四格单画卷），
            # 不并入密度告警，单独标注供 LLM 核对
            formatted_pages.append(f"{s.get('id')}({s.get('kind')},{len(elems)}元素)")
        elif len(elems) > max_n:
            density_bad.append(f"{s.get('id')}({len(elems)}个)")
        if not elems:
            empty_pages.append(str(s.get("id")))
    if formatted_pages:
        lines.append(f"- 版式栏目页（元素少属正常，勿按密度扣分）：{', '.join(formatted_pages)}")
    if density_bad:
        lines.append(f"- ⚠ 元素超密度上限({max_n})的页：{', '.join(density_bad)}")
    if empty_pages:
        lines.append(f"- ⚠ 空元素页：{', '.join(empty_pages)}")
    if not density_bad and not empty_pages:
        lines.append(f"- 每页元素数均在学段上限（{max_n}）内，无空页")
    return "\n".join(lines)


def _sample_pages(doc: dict) -> str:
    """内容层抽查：前 2 页 + 固定种子随机 2 页的完整 elements。"""
    slides = doc.get("slides", [])
    idx = [0, 1]
    if len(slides) > 4:
        rng = random.Random(42)
        pool = list(range(2, len(slides)))
        idx += sorted(rng.sample(pool, min(2, len(pool))))
    idx = [i for i in dict.fromkeys(idx) if i < len(slides)]
    parts = []
    for i in idx:
        parts.append(json.dumps(slides[i], ensure_ascii=False, indent=1))
    return "\n\n".join(parts)


def _compute_pass(review: dict) -> bool:
    """pass = 无 issues 且四维均分 ≥4。LLM 漏给 pass 字段时程序兜底。"""
    scores = review.get("scores") or {}
    vals = [v for v in scores.values() if isinstance(v, (int, float))]
    avg = sum(vals) / len(vals) if vals else 0
    issues = review.get("issues") or []
    return bool(vals) and avg >= 4 and not issues


def _make_review_node(gateway: Any, emitter: Callable[[dict], None] | None,
                      model_kwargs: dict | None = None):
    """review 节点工厂：结构预检 + 抽查页喂 LLM 评分，发 review 帧。"""
    model_kwargs = model_kwargs or {}

    async def review(state: YuwenState) -> dict:
        _step(emitter, "review", "AI 质量审查", "running")

        visited = list(state.get("nodes_visited") or [])
        if "review" not in visited:
            visited.append("review")

        doc = state.get("yuwen_content") or {}
        rounds = int(state.get("yuwen_revise_rounds") or 0)
        if not doc.get("slides"):
            # 无内容可审：视为 pass 直接放行（gen_slides 全失败的情况由
            # yuwen_error 在 report 里体现，不在这阻断）
            _step(emitter, "review", "AI 质量审查", "done", "无内容，跳过审查")
            return {"yuwen_review": {"scores": {}, "issues": [], "pass": True},
                    "nodes_visited": visited}

        system_prompt = SYSTEM_REVIEW.format(
            structure_report=_structure_report(doc),
            sample_pages=_sample_pages(doc),
        )
        review_result: dict | None = None
        err = ""
        for attempt in range(2):
            try:
                resp = _call_llm(
                    gateway, "chat",
                    [ChatMessage("system", system_prompt),
                     ChatMessage("user", "请输出审查 JSON。")],
                    model_kwargs, temperature=0.2, json_mode=True)
                parsed = _parse_llm_json(resp.content)
                if (isinstance(parsed, dict)
                        and isinstance(parsed.get("scores"), dict)):
                    parsed.setdefault("issues", [])
                    parsed["pass"] = _compute_pass(parsed)
                    review_result = parsed
                    break
                err = "审查输出缺 scores 对象"
            except Exception as exc:  # noqa: BLE001 - 审查失败不阻断，视为 pass
                err = str(exc)

        if review_result is None:
            # 审查不可用 ≠ 课件不合格——降级放行（标注原因），
            # 阻断权交给 render 前的最后校验兜底
            review_result = {"scores": {}, "issues": [], "pass": True,
                             "error": f"审查失败，降级放行：{err[:80]}"}
            _step(emitter, "review", "AI 质量审查", "done",
                  review_result["error"])
        else:
            n_issues = len(review_result.get("issues", []))
            _step(emitter, "review", "AI 质量审查", "done",
                  f"评分均 {sum(v for v in review_result['scores'].values() if isinstance(v,(int,float)))/max(1,len(review_result['scores'])):.1f}"
                  f"，{n_issues} 个问题，{'通过' if review_result['pass'] else '需修订'}"
                  f"（第 {rounds + 1} 轮）")

        _emit(emitter, {"type": "review", "review": review_result})
        return {"yuwen_review": review_result, "nodes_visited": visited}

    return review
