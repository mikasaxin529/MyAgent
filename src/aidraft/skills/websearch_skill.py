"""WebSearch Skill：联网搜索能力（Tavily 后端）。

把"联网搜索"封装为标准化 AI Skill，与 repo/cicd/issue 同范式：
- env 读凭证（TAVILY_API_KEY），缺失优雅降级返回提示字符串而非崩溃。
- 惰性 import tavily-python：顶层 import 不受影响，调用时才加载。
- 方法返回 str（与其它 Skill 一致），便于直接拼进 LLM prompt。

设计要点（为什么单独一个 WebSearchSkill 而非直接在 agent 里调）：
1. 低代码 Skill 框架一致性：搜索能力应与 repo/cicd 等同层发现、同层调度，
   registry.all_specs() 聚合后既可喂给 langchain Tool 也可暴露成 MCP tool。
2. 凭证/降级集中：key 缺失时返回明确提示，上层 agent 看到提示即可改走
   不联网的分支，不会因一个搜索失败拖垮整条 agent 链。
3. 可替换：Tavily 换成 Serper/DuckDuckGo 只需改本类实现，registry 与
   agent 完全复用——"对扩展开放、对修改关闭"。
"""
from __future__ import annotations

import os
from typing import Any

from .registry import SkillSpec


class WebSearchSkill:
    """联网搜索 Skill：把 Tavily 搜索封装为标准化 AI 能力。

    凭证策略（与 RepoSkill 一致）：构造时从环境变量读 TAVILY_API_KEY，
    缺失则标记不可用，调用时降级返回提示字符串。
    """

    name = "websearch"

    def __init__(self, api_key: str = "") -> None:
        """初始化搜索 Skill。

        Args:
            api_key: Tavily API Key。优先从环境变量 TAVILY_API_KEY 读取；
                构造函数显式传入可覆盖（便于测试）。
        """
        self._api_key: str = api_key or os.getenv("TAVILY_API_KEY", "")
        self._client: Any = None
        if self._api_key:
            # 惰性初始化 client：避免 import 时即报错（tavily-python 缺失也能 import 本类）
            try:
                from tavily import TavilyClient  # 惰性 import
                self._client = TavilyClient(api_key=self._api_key)
            except Exception:  # noqa: BLE001 - client 构造失败降级，不崩
                self._client = None

    @property
    def available(self) -> bool:
        """是否可用：有 key 且 client 初始化成功。"""
        return self._client is not None

    # ------------------------------------------------------------------
    # 能力实现
    # ------------------------------------------------------------------
    def search(self, query: str, max_results: int = 5, time_range: str = "month") -> str:
        """联网搜索，返回可读的结果摘要文本。

        为什么默认 time_range="month"：用户用 websearch 就是要"最新"信息（最新版本/
        近期新闻/实时数据），限定近一个月可过滤掉陈旧页面，确保返回的是网络实时
        结果而非 LLM 训练时的旧数据。如需更宽时间窗传 "" 关闭。

        Args:
            query: 搜索关键词。
            max_results: 最多返回条数（Tavily 默认 5）。
            time_range: 时间范围 "day"/"week"/"month"/"year"，空串表示不限。
                Tavily 据此过滤近期网页，保证结果新鲜。

        Returns:
            拼接好的搜索结果文本（标题 + url + 摘要）；不可用时返回降级提示。
        """
        if self._client is None:
            # 凭证缺失降级：返回明确提示，上层 agent 据此改走不联网分支。
            return (
                "[websearch] Tavily 未配置（缺少 TAVILY_API_KEY 或 client 初始化失败），"
                "无法联网搜索。请在 .env 设置 TAVILY_API_KEY 后重试。"
            )
        try:
            # 组装 Tavily 入参：search_depth=advanced 取更全内容，time_range 过滤近期。
            kwargs: dict = {
                "query": query,
                "max_results": max_results,
                "search_depth": "advanced",  # advanced 返回更长正文，便于后续总结
            }
            if time_range:
                kwargs["time_range"] = time_range
            resp = self._client.search(**kwargs)
            results = resp.get("results", []) if isinstance(resp, dict) else []
            if not results:
                return f"[websearch] 未搜到与 '{query}' 相关的结果。"
            # 拼成可读文本喂给后续 LLM 总结：每条含标题、url、内容摘要（截断防超长）。
            lines = [f"搜索关键词：{query}（共 {len(results)} 条，time_range={time_range or '不限'}）"]
            for i, item in enumerate(results, 1):
                title = item.get("title", "")
                url = item.get("url", "")
                content = (item.get("content", "") or "")[:400]
                lines.append(f"{i}. {title}\n   {url}\n   {content}")
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001 - 搜索失败降级，不崩
            return f"[websearch] 搜索失败：{exc!r}"

    def _tool_search(self, query: str, max_results: int = 5, time_range: str = "month") -> dict:
        """原生 function-calling 用的结构化搜索：返回 {content, search_items}。

        content 是拼好的可读文本（喂 LLM 当 ToolMessage content）；
        search_items 是逐条 [{title,url,snippet}]，ToolNode 经 SSE 逐条 push
        给前端 tool-block-sources 卡片（对齐 ChatFlow WebSearchFormatter 逐条 search_item）。

        与 search() 的区别：search() 返回 str（兼容 CLI/旧 executor）；
        _tool_search 返回 dict（供新 ToolNode 结构化分流）。
        """
        if self._client is None:
            return {
                "content": "[websearch] Tavily 未配置（缺少 TAVILY_API_KEY），无法联网搜索。",
                "search_items": [],
                "error": True,
            }
        try:
            kwargs: dict = {
                "query": query,
                "max_results": max_results,
                "search_depth": "advanced",
            }
            if time_range:
                kwargs["time_range"] = time_range
            resp = self._client.search(**kwargs)
            results = resp.get("results", []) if isinstance(resp, dict) else []
            if not results:
                return {
                    "content": f"[websearch] 未搜到与 '{query}' 相关的结果。",
                    "search_items": [],
                }
            items: list[dict] = []
            lines = [f"搜索关键词：{query}（共 {len(results)} 条，time_range={time_range or '不限'}）"]
            for i, item in enumerate(results, 1):
                title = item.get("title", "")
                url = item.get("url", "")
                raw_content = item.get("content", "") or ""
                lines.append(f"{i}. {title}\n   {url}\n   {raw_content[:400]}")
                items.append({"title": title, "url": url, "snippet": raw_content[:200]})
            return {"content": "\n".join(lines), "search_items": items}
        except Exception as exc:  # noqa: BLE001
            return {
                "content": f"[websearch] 搜索失败：{exc!r}",
                "search_items": [],
                "error": True,
            }

    def fetch_page(self, url: str) -> str:
        """抓取单个网页正文（Tavily 的 extract 能力）。

        用于"搜索到候选后取全文"场景。Tavily 不支持 extract 时降级提示。

        Args:
            url: 目标网页 URL。

        Returns:
            网页正文文本；不可用或失败返回降级提示。
        """
        if self._client is None:
            return "[websearch] Tavily 未配置，无法抓取网页。"
        try:
            # Tavily extract 接口：传 url 取正文。
            resp = self._client.extract(urls=[url])
            results = resp.get("results", []) if isinstance(resp, dict) else []
            if not results:
                return f"[websearch] 未能从 {url} 提取正文。"
            text = results[0].get("raw_content", "") or results[0].get("text", "")
            return text[:2000]  # 截断防超长
        except Exception as exc:  # noqa: BLE001
            return f"[websearch] 抓取网页失败：{exc!r}"

    # ------------------------------------------------------------------
    # Skill 能力清单（供 registry.all_specs 聚合，可暴露成 MCP tool）
    # ------------------------------------------------------------------
    def specs(self) -> list[SkillSpec]:
        """暴露搜索能力为 SkillSpec，供 agent 发现与调度。"""
        return [
            SkillSpec(
                name="websearch",
                description="联网搜索最新信息。入参 query（搜索词），返回结果摘要文本。",
                func=self._tool_search,
                guidance=(
                    "联网搜索工具：用户要最新信息（新闻/版本/实时数据/近期事件）时调用。"
                    "入参 query 搜索词、max_results 条数（默认5）、time_range 时间窗"
                    "（day/week/month/year，默认 month 取最新）。返回结构化结果含逐条标题/url/摘要。"
                    "何时不用：用户只问概念/写代码/闲聊，或问题在你自身知识范围内且不要求最新——"
                    "直接答即可，不必调。"
                ),
                schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"},
                        "max_results": {
                            "type": "integer",
                            "description": "最大结果数",
                            "default": 5,
                        },
                        "time_range": {
                            "type": "string",
                            "description": "时间范围 day/week/month/year",
                            "default": "month",
                        },
                    },
                    "required": ["query"],
                },
            ),
            SkillSpec(
                name="websearch_fetch_page",
                description="抓取指定 URL 网页正文。入参 url。",
                func=self.fetch_page,
                schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "目标网页 URL"},
                    },
                    "required": ["url"],
                },
            ),
        ]
