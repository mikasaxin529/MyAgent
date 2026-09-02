"""research 节点：大纲生成前的联网资料搜索（可选增强，M2）。

管线位置：extract_params → **research** → gen_outline。
搜两路："<课文名> 教学设计"（栏目结构参考）+ "<课文名> 课文原文"
（内容准确性——LLM 记不全课文原文，古诗尤甚）。结果以
"## 联网参考资料（生成大纲时参考）" 段拼进 gen_outline 的 user prompt。

降级链（任何一环失败都不阻断主流程）：
- 无 TAVILY_API_KEY / Tavily client 不可用 → step done "未配置搜索，跳过"
- 搜索返回降级提示串（[websearch] 开头）→ 视为无结果
- 搜索异常 → step error 但 outline 照常生成（纯 LLM 知识兜底）

跨轮缓存：结果落盘 state.json 的 yuwen_research 字段。用户改纲/
切主题（confirm 路径 C）重走 LLM 不重搜——网上一份资料够用，
Tavily 按次计费。params 变了（新课文）自然换 session 目录不命中。
"""
from __future__ import annotations

import time
from typing import Callable

from ..state import YuwenState, _load_state, _save_state, _step

# 每路搜索的结果字符上限：资料是"参考"不是"底稿"，太长挤占 outline
# prompt 预算还教模型抄网文（教学设计要原创，只借结构思路）。
_MAX_CHARS_PER_QUERY = 1500
# 搜索结果超龄即失效：课文不会变，但"教学设计"的时效性（新课标、
# 新教法讨论）以月计。7 天内同一 session 不重搜。
_RESEARCH_TTL_SECONDS = 7 * 24 * 3600


def _looks_like_degraded(text: str) -> bool:
    """Tavily 降级返回串判定（"[websearch]" 开头的提示而非搜索结果）。"""
    return not text.strip() or text.strip().startswith("[websearch]")


def _fresh(research: dict) -> bool:
    """盘上资料是否仍新鲜（TTL 内且非降级空结果）。"""
    if not research or not research.get("content"):
        return False
    ts = research.get("ts") or 0
    return (time.time() - ts) < _RESEARCH_TTL_SECONDS


def _search_queries(params: dict) -> list[str]:
    """由参数派生两路搜索词。"""
    title = str(params.get("title") or "").strip()
    if not title:
        return []
    grade = params.get("grade") or ""
    return [
        f"{title} {grade}年级 教学设计",
        f"{title} 课文原文",
    ]


def _make_research_node(emitter: Callable[[dict], None] | None):
    """research 节点工厂：联网搜教学参考，结果存 state 供 gen_outline 用。"""

    async def research(state: YuwenState) -> dict:
        visited = list(state.get("nodes_visited") or [])
        if "research" not in visited:
            visited.append("research")

        params = state.get("yuwen_params") or {}
        queries = _search_queries(params)
        if not queries:
            # 没课文名（正常流程到这里 params 已齐备，防御兜底）
            _step(emitter, "research", "联网搜索", "done", "无课文名，跳过")
            return {"yuwen_research": {}, "nodes_visited": visited}

        # 盘上有新鲜资料直接复用（confirm 改纲轮重进本节点不重搜）
        disk = _load_state(params)
        cached = disk.get("yuwen_research") or {}
        if _fresh(cached):
            n = len(cached.get("sources") or [])
            _step(emitter, "research", "联网搜索", "done",
                  f"复用 {time.strftime('%m-%d %H:%M', time.localtime(cached['ts']))} 的资料（{n} 条来源）")
            return {"yuwen_research": cached, "nodes_visited": visited}

        # 惰性取 WebSearchSkill（经 registry 构造链注入太重，直接实例化——
        # 凭证策略一致：env 读 TAVILY_API_KEY，缺失 available=False 降级）
        try:
            from ....skills.websearch_skill import WebSearchSkill
            skill = WebSearchSkill()
        except Exception as exc:  # noqa: BLE001 - import 失败按不可用降级
            _step(emitter, "research", "联网搜索", "error",
                  f"搜索组件加载失败：{exc}，大纲走纯 LLM 生成")
            return {"yuwen_research": {}, "nodes_visited": visited}

        if not skill.available:
            _step(emitter, "research", "联网搜索", "done",
                  "未配置 TAVILY_API_KEY，跳过联网搜索（大纲走纯 LLM 生成）")
            return {"yuwen_research": {}, "nodes_visited": visited}

        _step(emitter, "research", "联网搜索", "running",
              f"{len(queries)} 路搜索：{'；'.join(queries)}")

        blocks: list[str] = []
        sources: list[dict] = []
        degraded = 0
        for q in queries:
            try:
                text = skill.search(q, max_results=3, time_range="year")
            except Exception as exc:  # noqa: BLE001 - 单路失败不影响另一路
                degraded += 1
                _step(emitter, "research", "联网搜索", "error",
                      f"「{q}」搜索失败：{exc}")
                continue
            if _looks_like_degraded(text):
                degraded += 1
                continue
            blocks.append(f"### {q}\n{text[:_MAX_CHARS_PER_QUERY]}")
            # 来源抽取：结果文本行格式 "N. 标题\n   url"，取 url 行
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("http") and len(sources) < 8:
                    sources.append({"query": q, "url": s})

        payload: dict = {}
        if blocks:
            payload = {"content": "\n\n".join(blocks),
                       "sources": sources, "ts": time.time()}
            _save_state(params, yuwen_research=payload)
            _step(emitter, "research", "联网搜索", "done",
                  f"搜到 {len(sources)} 条参考资料"
                  + ("（部分查询失败）" if degraded else ""))
        else:
            _step(emitter, "research", "联网搜索", "done",
                  "搜索无结果，大纲走纯 LLM 生成")
        return {"yuwen_research": payload, "nodes_visited": visited}

    return research
