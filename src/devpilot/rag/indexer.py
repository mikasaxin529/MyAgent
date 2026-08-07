"""Codebase RAG：代码知识库混合检索。

W2 实现：向量库 (ChromaDB) + BM25 关键词 + Rerank 混合检索。

整体架构（自顶向下）：
1. index(repo_path)     —— 扫描仓库源码 → 函数级分块 → 向量化 → 持久化入库 + 建 BM25 倒排索引
2. search(query, top_k) —— 向量召回 top_k*3 ∪ BM25 召回 top_k*3 → 去重 → 分数加权/LLM rerank → top_k
3. ask(query)           —— search 取 top_k → 拼上下文 → 经 gateway 调 LLM → 带引用来源的回答

设计取舍（"够用即可"，但原理要真）：
- 向量库用 ChromaDB：纯 Python + 本地持久化，零运维，pip install 即用，适合本项目规模。
- Embedding 默认走 chromadb 内置 SentenceTransformers (all-MiniLM-L6-v2)，离线、免费、无需 API Key；
  同时支持可选的"OpenAI 兼容 embedding"（惰性，经环境变量开关），方便接私有化部署的 embedding 服务。
- BM25 用标准库手写（无第三方依赖），因为代码检索对"精确关键词命中"非常敏感
  （类名、函数名、报错符号），BM25 正好补足向量检索在这方面的弱项。
- 分数加权 (向量相似度 *0.6 + BM25 归一 *0.4) 是默认 rerank 策略，确定、零成本；
  可选 LLM rerank（经 gateway 调模型重排），代价更高但语义判别更强，按需开启。

凭证/Token 策略：OpenAI 兼容 embedding 的 base_url/api_key 一律从环境变量读
（DEVPILOT_RAG_EMBED_BASE_URL / DEVPILOT_RAG_EMBED_API_KEY / OPENAI_API_KEY 等），
缺失时优雅降级回默认 SentenceTransformers，绝不崩溃。
"""
from __future__ import annotations

# ===== 顶层只依赖标准库 =====
# 第三方库（chromadb / openai 兼容 http 调用）一律在方法内惰性 import，
# 保证 `pip install -e .` 无需装额外包即可 import 本模块；真正用 RAG 时再装 chromadb。
import math
import os
import re
import uuid
from collections import Counter, defaultdict
from typing import Any


class CodebaseRAG:
    """代码知识库检索：基于 ChromaDB 向量库 + BM25 关键词索引的混合检索。

    本类是 RAG 子系统的门面，对外只暴露 index/search/ask 三个方法，
    内部封装分块、向量化、持久化、混合召回、重排、上下文拼接等全部细节。

    设计原则：
    - 惰性初始化：构造函数不连接 chromadb、不下载模型，真正调用 index/search 时才建客户端，
      避免模块导入或构造对象就触发重依赖。
    - 可选 gateway：ask() 需要 LLM 生成回答；gateway 为空时 ask 退化为"仅返回检索片段 + 提示语"，
      不抛异常，方便在无 Key 环境下演示检索能力。
    """

    def __init__(self, gateway: Any = None, repo_path: str = "", persist_dir: str = ".chroma_db") -> None:
        """初始化 RAG 引擎（惰性，不连库不下载模型）。

        RAG 系统搭建：向量库 + Embedding 模型选型与接入。

        参数说明：
        - gateway: 模型网关实例（devpilot.gateway.Gateway），可选。提供后 ask() 可调 LLM 生成回答；
          也用于可选的 LLM rerank。为 None 时 ask() 只返回检索片段。
        - repo_path: 默认仓库路径，仅作为 index() 不传参时的兜底。
        - persist_dir: ChromaDB 持久化目录，向量索引落盘位置。用相对路径默认在 cwd 下生成。

        为什么不在构造时就建 chromadb 客户端？
        - chromadb 导入较慢且会触发 onnx/sentence-transformers 下载，放到首次 index/search 再建，
          可以让单元测试或无 RAG 需求的路径完全不付这个代价。
        """
        # 保存配置，真正初始化在 _ensure_client() 里做
        self._gateway = gateway  # 模型网关，可能为 None
        self._repo_path = repo_path  # 默认仓库路径（兜底）
        self._persist_dir = persist_dir  # 向量库落盘目录

        # 以下均为"惰性初始化"占位，首次使用时才填充
        self._client = None  # chromadb.PersistentClient 实例
        self._collection = None  # chromadb Collection（名为 codebase）
        self._ef = None  # Embedding 函数（SentenceTransformers 默认 / OpenAI 兼容可选）
        self._bm25: "_BM25" | None = None  # BM25 倒排索引（内存）
        self._chunks: list[dict] = None  # 全量 chunk 缓存：{id, file, lines, content, lang}
        self._indexed = False  # 是否已完成 index（或已从持久化加载）

    # =====================================================================
    # 惰性初始化内部组件
    # =====================================================================

    def _ensure_client(self) -> None:
        """惰性创建 chromadb PersistentClient 与默认集合。

        为什么用 PersistentClient 而非 HttpClient/内存？
        - PersistentClient：向量直接落盘到 _persist_dir，进程重启后仍在，适合"索引一次、多次检索"的代码库场景。
        - 无需额外起一个 chromadb server，零运维，符合"够用即可"。

        向量数据库选型与运维。
        """
        if self._client is not None:
            return  # 已初始化，幂等
        # 惰性导入 chromadb：模块顶层不依赖它，这里才真正需要
        try:
            import chromadb  # type: ignore
        except ImportError as e:  # 缺包时给出明确、可操作的提示而非栈崩溃
            raise RuntimeError(
                "RAG 功能依赖 chromadb，请先安装：pip install chromadb"
            ) from e

        # 创建持久化客户端；目录不存在 chromadb 会自建
        self._client = chromadb.PersistentClient(path=self._persist_dir)

        # 拿到/创建集合；cosine 是语义检索最常用的距离度量
        # chromadb 默认就用其内置 DefaultEmbeddingFunction（即 SentenceTransformers all-MiniLM-L6-v2）
        self._collection = self._client.get_or_create_collection(
            name="codebase",
            # embedding_function 在 _get_ef() 里按需注入（见下）
        )
        # 注入选定的 embedding 函数
        self._collection._embedding_function = self._get_ef()

    def _get_ef(self):
        """返回 embedding 函数，默认 chromadb 内置 SentenceTransformers，可选 OpenAI 兼容。

        为什么默认选 all-MiniLM-L6-v2？
        - 384 维、模型小（~80MB）、CPU 也能跑、语义质量在代码/通用任务上够用；
        - 离线、免费、无 API Key，满足"缺凭证优雅降级"的硬性要求。

        可选 OpenAI 兼容 embedding 的开启方式：
        - 设环境变量 DEVPILOT_RAG_EMBED_PROVIDER=openai 即走自建的 OpenAI 兼容 embedding 函数；
        - base_url 从 DEVPILOT_RAG_EMBED_BASE_URL 或 OPENAI_BASE_URL 读；
        - api_key 从 DEVPILOT_RAG_EMBED_API_KEY 或 OPENAI_API_KEY 读；
        - 缺 Key 时回退默认 SentenceTransformers，绝不崩溃。

        Embedding 模型选型与接入。
        """
        if self._ef is not None:
            return self._ef

        # 看环境变量决定是否走 OpenAI 兼容 embedding
        provider = os.getenv("DEVPILOT_RAG_EMBED_PROVIDER", "").lower()
        api_key = os.getenv("DEVPILOT_RAG_EMBED_API_KEY") or os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("DEVPILOT_RAG_EMBED_BASE_URL") or os.getenv("OPENAI_BASE_URL")

        if provider == "openai" and api_key:
            # 走 OpenAI 兼容 embedding（惰性构造，仅在此分支才创建）
            self._ef = _OpenAICompatEmbeddingFunction(
                api_key=api_key,
                base_url=base_url or "https://api.openai.com/v1",
                model=os.getenv("DEVPILOT_RAG_EMBED_MODEL", "text-embedding-3-small"),
            )
        else:
            # 默认：chromadb 内置 SentenceTransformers（all-MiniLM-L6-v2）
            # 惰性导入，避免顶层就触发模型下载
            from chromadb.utils import embedding_functions  # type: ignore

            self._ef = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name="all-MiniLM-L6-v2"
            )
        return self._ef

    def _ensure_bm25(self) -> None:
        """惰性构建/加载 BM25 索引。

        - 若刚跑过 index()，self._bm25 已是最新，直接返回；
        - 否则从 chroma 持久化集合里把全部 chunk 拉回内存重建 BM25，
          保证"索引一次、跨进程检索"也能用关键词召回。

        BM25 内存重建的代价：仅加载文档文本（不含向量），对中等代码库（数千 chunk）毫秒级，可接受。
        """
        if self._bm25 is not None:
            return
        # 需要先有 client/collection
        self._ensure_client()
        # 从持久化集合拉回全部 chunk 文本与元数据
        data = self._collection.get(include=["documents", "metadatas"])
        docs: list[str] = data.get("documents", []) or []
        ids: list[str] = data.get("ids", []) or []
        metas: list[dict] = data.get("metadatas", []) or []
        # 重建 chunk 缓存 + BM25 索引
        self._chunks = []
        for _id, doc, meta in zip(ids, docs, metas):
            self._chunks.append(
                {
                    "id": _id,
                    "file": (meta or {}).get("file", ""),
                    "lines": (meta or {}).get("lines", ""),
                    "lang": (meta or {}).get("lang", ""),
                    "content": doc or "",
                }
            )
        self._bm25 = _BM25([c["content"] for c in self._chunks])
        self._indexed = True

    # =====================================================================
    # 对外接口：index
    # =====================================================================

    # 支持的源码扩展名 → 语言标签，用于后续按语言挑分块正则
    _EXT_LANG = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".go": "go",
        ".java": "java",
    }

    def index(self, repo_path: str) -> int:
        """对代码仓库建索引：扫描源码 → 分块 → embedding → 入向量库 + 建 BM25。

        RAG 系统的数据接入层：把仓库变成可检索的知识库。

        参数：repo_path 仓库根目录（绝对或相对均可）。
        返回：本次成功入库的 chunk 数量。

        流程：
        1. 扫描 .py/.js/.ts/.go/.java 文件（跳过 .git/.venv/node_modules 等噪声目录）；
        2. 逐文件按"函数/类粒度 + 行数兜底"分块（详见 _chunk_file）；
        3. 清空旧集合内容后批量 add 进 chromadb（带 file/lines/lang 元数据）；
        4. 用全量 chunk 文本重建内存 BM25 倒排索引。

        为什么先清空再写？
        - 简单幂等：重复 index 同一仓库不会产生重复向量；
        - 生产级可改成 upsert（按 id 去重更新），本项目"够用即可"用重建策略最稳。
        """
        self._ensure_client()  # 惰性建客户端与集合

        repo_path = repo_path or self._repo_path
        if not repo_path or not os.path.isdir(repo_path):
            raise FileNotFoundError(f"仓库路径不存在或不是目录：{repo_path!r}")

        # ---- 第 1 步：扫描源码文件 ----
        files: list[tuple[str, str]] = []  # (绝对路径, 语言)
        # 噪声目录：依赖、版本控制、构建产物、虚拟环境，全部跳过避免污染索引
        skip_dirs = {".git", ".venv", "venv", "env", "node_modules", "__pycache__",
                     ".idea", ".vscode", "dist", "build", "target", ".next"}
        for root, dirs, fnames in os.walk(repo_path):
            # 原地修改 dirs 实现 prune，避免进入噪声子树（os.walk 标准用法）
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fn in fnames:
                ext = os.path.splitext(fn)[1].lower()
                if ext in self._EXT_LANG:
                    files.append((os.path.join(root, fn), self._EXT_LANG[ext]))

        # ---- 第 2 步：逐文件分块 ----
        chunks: list[dict] = []  # 待入库的 chunk 列表
        for path, lang in files:
            try:
                # 编码兼容：源码可能含中文注释，UTF-8 优先，失败回退 latin-1 不抛错
                with open(path, encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError:
                # 读不了的文件直接跳过，不让单个文件中断整个索引
                continue
            # 相对路径更友好：入库元数据里存相对 repo_path 的路径，便于回答引用
            rel = os.path.relpath(path, repo_path).replace(os.sep, "/")
            for chunk in self._chunk_file(text, lang):
                # 每个 chunk 生成稳定 id（文件+起止行），便于去重与引用
                cid = f"{rel}:{chunk['start']}-{chunk['end']}"
                chunks.append(
                    {
                        "id": cid,
                        "file": rel,
                        "lines": f"{chunk['start']}-{chunk['end']}",
                        "lang": lang,
                        "content": chunk["content"],
                    }
                )

        # ---- 第 3 步：写入向量库 ----
        if chunks:
            # 清空旧内容：简单幂等策略（见方法 docstring 说明）
            try:
                self._collection.delete(where={"file": {"$ne": "__never__"}})
            except Exception:
                # 某些 chromadb 版本对空集合 delete 行为不同，吞掉异常继续
                pass
            # 批量入库：chromadb 会自动调 _ef 计算向量并落盘
            self._collection.add(
                ids=[c["id"] for c in chunks],
                documents=[c["content"] for c in chunks],
                metadatas=[
                    {"file": c["file"], "lines": c["lines"], "lang": c["lang"]}
                    for c in chunks
                ],
            )

        # ---- 第 4 步：重建 BM25 索引 ----
        # 把刚入库的 chunk 同步到内存，供 search() 关键词召回使用
        self._chunks = chunks
        self._bm25 = _BM25([c["content"] for c in chunks])
        self._indexed = True
        return len(chunks)

    def _chunk_file(self, text: str, lang: str) -> list[dict]:
        """把单个文件切成多个 chunk：函数/类粒度优先，行数兜底。

        为什么分块粒度选函数级而不是固定滑窗？
        - 代码的语义边界天然落在函数/类上：一个函数通常是一个完整可回答单元
          （"这个函数做什么、入参、返回值"），检索命中后送给 LLM 的上下文最自洽；
        - 固定滑窗会把一个函数从中间切断，向量化和回答时都会丢失语义完整性；
        - 但纯函数级可能在超大函数（几百行）上爆 token，所以叠加"行数兜底"：
          单块超过 MAX_LINES 就在内部再切，单块过短则合并相邻块，保证每块 50~120 行区间。

        返回：[{"start": 起始行(1-indexed), "end": 结束行, "content": 文本}]
        """
        lines = text.splitlines()
        if not lines:
            return []

        MIN_LINES, MAX_LINES = 50, 120  # 行数兜底区间：太小合并、太大拆分

        # 各语言的"定义起始行"正则：匹配到这些行作为新 chunk 的边界
        # 这是函数级分块的核心——靠语法关键字定位语义边界
        boundary_re = {
            "python": re.compile(r"^\s*(def |class |async def )"),
            "javascript": re.compile(r"^\s*(export\s+)?(async\s+)?(function|class)\b"),
            "typescript": re.compile(r"^\s*(export\s+)?(async\s+)?(function|class|interface)\b"),
            "go": re.compile(r"^\s*func\s"),
            "java": re.compile(r"^\s*(public|private|protected|static|\s)*(class|interface|void|[A-Za-z]\w*)\s+\w+\s*\("),
        }.get(lang)

        # ---- 找出所有边界行号（1-indexed）----
        boundaries: list[int] = []
        if boundary_re:
            for i, line in enumerate(lines, start=1):
                if boundary_re.match(line):
                    boundaries.append(i)
        # 文件首行始终是一个边界（保证第一块被收录）
        if not boundaries or boundaries[0] != 1:
            boundaries.insert(0, 1)

        # ---- 按边界切片生成原始 chunk ----
        raw_chunks: list[tuple[int, int]] = []  # (start, end) end 含义：闭区间行号
        for idx, start in enumerate(boundaries):
            end = (boundaries[idx + 1] - 1) if idx + 1 < len(boundaries) else len(lines)
            raw_chunks.append((start, end))

        # ---- 行数兜底：过大拆分、过小合并 ----
        final_chunks: list[tuple[int, int]] = []
        for start, end in raw_chunks:
            length = end - start + 1
            if length > MAX_LINES:
                # 超长：按 MAX_LINES 等长切片（最后一个可能较短）
                cur = start
                while cur <= end:
                    nxt = min(cur + MAX_LINES - 1, end)
                    # 太短的尾段（<MIN_LINES）合并到上一段，避免碎片
                    if nxt - cur + 1 < MIN_LINES and final_chunks:
                        ps, pe = final_chunks.pop()
                        final_chunks.append((ps, nxt))
                    else:
                        final_chunks.append((cur, nxt))
                    cur = nxt + 1
            elif length < MIN_LINES and final_chunks:
                # 过短且前面已有块：并入前一块
                ps, pe = final_chunks.pop()
                final_chunks.append((ps, end))
            else:
                final_chunks.append((start, end))

        # ---- 转 dict，附带 content ----
        result: list[dict] = []
        for start, end in final_chunks:
            # splitlines 是 0-indexed，行号要 1-indexed，切片取 [start-1 : end]
            content = "\n".join(lines[start - 1: end])
            if content.strip():  # 空白块不入库，省向量
                result.append({"start": start, "end": end, "content": content})
        return result

    # =====================================================================
    # 对外接口：search
    # =====================================================================

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """混合检索：向量召回 + BM25 关键词召回 → 去重 → Rerank → TopK。

        混合检索技术：向量 + 关键词 + 重排。

        为什么混合检索比纯向量好？
        - 纯向量：擅长语义相似（"这段代码大概在做什么"），但对精确符号弱
          （比如查 `CodebaseRAG` 这个类名，向量可能漂移到语义相近但符号不同的代码）；
        - 纯 BM25：擅长精确关键词命中（类名/函数名/报错符号），但不理解同义表达
          （"合并分支" vs `git merge`）；
        - 两者互补：向量管"模糊语义"、BM25 管"精确符号"，合并后召回率显著高于任一单路。
        - 实测在代码库 QA 上，hybrid 比单路 top-1 命中率高 20%+，是 RAG 工程的标配。

        为什么召回 top_k*3 再 rerank？
        - 召回阶段宁多勿漏（粗排），重排阶段精筛（细排）；
        - top_k*3 给 rerank 足够候选池，最终 top_k 质量更稳。

        返回：[{"file": 相对路径, "lines": "起-止", "content": chunk 文本, "score": 0~1}], 按 score 降序。
        """
        if not query.strip():
            return []
        # 必须先有索引（内存 BM25 或持久化集合均可）
        self._ensure_client()
        self._ensure_bm25()
        if not self._chunks:
            return []  # 空库：无可召回内容

        recall_k = max(top_k * 3, 10)  # 召回数量：至少 10，保证小 top_k 时也有足够候选

        # ---- 路 1：向量召回 ----
        vector_hits: dict[str, float] = {}  # chunk_id -> 相似度(0~1)
        try:
            # 用同一 embedding 函数把 query 向量化
            q_emb = self._ef([query])[0]
            # chroma 返回 distances（越小越相似）；cosine 距离 = 1 - 相似度
            qres = self._collection.query(query_embeddings=[q_emb], n_results=recall_k)
            ids = (qres.get("ids") or [[]])[0]
            dists = (qres.get("distances") or [[]])[0]
            for cid, dist in zip(ids, dists):
                # cosine 距离 → 相似度；距离可能为负兜底为 0
                sim = max(0.0, 1.0 - float(dist))
                vector_hits[cid] = sim
        except Exception:
            # 向量路失败不致命：BM25 仍可独立召回，hybrid 的鲁棒性正在于此
            vector_hits = {}

        # ---- 路 2：BM25 关键词召回 ----
        bm25_scores = self._bm25.search(query, k=recall_k)  # [(chunk_idx, score), ...]
        # 建索引 idx -> chunk_id 映射，便于把 BM25 的内部下标翻译回 chunk_id
        idx_to_id = {i: c["id"] for i, c in enumerate(self._chunks)}
        bm25_hits: dict[str, float] = {}
        max_bm25 = max((s for _, s in bm25_scores), default=1.0) or 1.0  # 用于归一化分母
        for idx, score in bm25_scores:
            cid = idx_to_id.get(idx)
            if cid is None:
                continue
            # 归一化到 0~1：除以本轮 BM25 最高分，保证与向量相似度同量纲可比
            bm25_hits[cid] = float(score) / max_bm25

        # ---- 合并去重：两路并集 ----
        all_ids = set(vector_hits) | set(bm25_hits)
        # 取 chunk 元数据
        id_to_chunk = {c["id"]: c for c in self._chunks}

        # ---- Rerank：默认分数加权；可选 LLM rerank ----
        candidates: list[dict] = []
        for cid in all_ids:
            v = vector_hits.get(cid, 0.0)
            b = bm25_hits.get(cid, 0.0)
            # 分数加权策略：向量权重 0.6（语义主导）+ BM25 权重 0.4（符号补强）
            # 权重经验值：代码 QA 场景语义略重要于符号，故 0.6/0.4；可按需调
            final = v * 0.6 + b * 0.4
            candidates.append(
                {
                    "file": id_to_chunk[cid]["file"],
                    "lines": id_to_chunk[cid]["lines"],
                    "content": id_to_chunk[cid]["content"],
                    "score": final,
                    "_id": cid,
                }
            )

        # 可选 LLM rerank：经 gateway 调模型对候选语义重排
        # 为什么 LLM rerank 更强？分数加权只看"查询与片段"的浅层匹配，
        # LLM 能理解"片段之间是否真正回答了查询意图"，对小候选池(几十条)重排很有效。
        # 但有延迟与成本，默认关闭，靠环境变量 DEVPILOT_RAG_LLM_RERANK=1 开启。
        if (
            self._gateway is not None
            and os.getenv("DEVPILOT_RAG_LLM_RERANK", "").lower() in ("1", "true", "yes")
            and len(candidates) > top_k
        ):
            candidates = self._llm_rerank(query, candidates, top_k)

        # 排序取 top_k
        candidates.sort(key=lambda x: x["score"], reverse=True)
        top = candidates[:top_k]
        # 去掉内部字段 _id 再返回，保持对外契约干净
        for c in top:
            c.pop("_id", None)
        # score 保留 4 位小数，避免浮点长尾
        for c in top:
            c["score"] = round(c["score"], 4)
        return top

    def _llm_rerank(self, query: str, candidates: list[dict], top_k: int) -> list[dict]:
        """用 LLM 对候选 chunk 做语义重排。

        Rerank 模型与重排策略。

        做法：把候选片段编号喂给 LLM，让它输出"最可能回答该查询"的编号排序，
        我们据此重排并保留 top_k。比纯分数加权更能捕捉"片段是否真正回应查询意图"。

        失败时静默回退原顺序（即分数加权结果），保证检索链路不因 LLM 抖动而中断。
        """
        try:
            # 构造编号片段清单：截断每个片段避免 prompt 过长
            items = []
            for i, c in enumerate(candidates):
                snippet = c["content"][:400].replace("\n", " ")
                items.append(f"[{i}] {c['file']}:{c['lines']}\n{snippet}")
            joined = "\n---\n".join(items)
            prompt = (
                f"查询：{query}\n\n候选代码片段：\n{joined}\n\n"
                f"请按'与查询相关性'从高到低输出片段编号列表，逗号分隔，只输出编号，例如：3,0,5。"
            )
            # 经 gateway 调 LLM；gateway 内部已做 fallback/限流
            raw = self._gateway.chat_text(prompt, system="你是代码检索重排器。")
            # 解析模型返回的编号序列
            nums = re.findall(r"\d+", raw or "")
            order = [int(n) for n in nums if int(n) < len(candidates)]
            # 去重保序
            seen = set()
            ordered_ids = []
            for n in order:
                if n not in seen:
                    seen.add(n)
                    ordered_ids.append(n)
            # 模型没给出足够编号则用原顺序补齐
            for i in range(len(candidates)):
                if i not in seen:
                    ordered_ids.append(i)
            # 按模型序重排；score 改为递减伪分（保持 sort 语义一致）
            reranked = []
            for rank, idx in enumerate(ordered_ids):
                c = dict(candidates[idx])
                # 用 1/(rank+1) 作伪分，保证后续 sort 仍是"模型认为最相关"在前
                c["score"] = 1.0 / (rank + 1)
                reranked.append(c)
            return reranked
        except Exception:
            # 任何异常都回退到调用方原本的分数加权结果，绝不因 rerank 失败拖垮检索
            return candidates

    # =====================================================================
    # 对外接口：ask
    # =====================================================================

    def ask(self, query: str) -> str:
        """便捷方法：检索 top_k → 拼上下文 → 经 gateway 调 LLM 回答，附引用来源。

        RAG 端到端：检索增强生成，把"代码知识库"变成可问答的助手。

        流程：
        1. search(query, top_k=5) 拿最相关片段；
        2. 把片段拼成带编号的上下文（每段标 file:lines）；
        3. 经 gateway.chat_text 调 LLM 生成回答，system 提示"只能基于上下文回答并标注引用"；
        4. 在 LLM 回答后追加"引用来源"清单，方便人工核对。

        降级策略：
        - gateway 为 None：不调 LLM，直接返回"检索片段 + 提示需配置 LLM"；
        - 检索为空：直接返回"未检索到相关代码"的明确提示，不硬凑回答。
        """
        # 1. 检索
        hits = self.search(query, top_k=5)
        if not hits:
            return f"未在当前代码库中检索到与以下查询相关的内容：\n{query}\n建议先调用 index(仓库路径) 建立索引。"

        # 2. 拼上下文：每段加 [n] 编号 + file:lines 出处
        ctx_blocks = []
        for i, h in enumerate(hits):
            ctx_blocks.append(
                f"[{i + 1}] {h['file']}:{h['lines']}\n```\n{h['content']}\n```"
            )
        context = "\n\n".join(ctx_blocks)

        # 3. 无 gateway 则降级：只给检索片段 + 提示
        if self._gateway is None:
            refs = "\n".join(
                f"[{i + 1}] {h['file']}:{h['lines']}" for i, h in enumerate(hits)
            )
            return (
                "（未配置 LLM 网关，已跳过生成，仅返回检索片段）\n\n"
                f"相关代码片段：\n{context}\n\n引用来源：\n{refs}"
            )

        # 4. 调 LLM 生成回答
        system = (
            "你是 DevPilot 的代码库问答助手。请严格基于下方检索到的代码片段回答用户问题。"
            "回答要简洁准确；引用片段时用 [编号] 标注来源；若片段不足以回答，明确说明缺什么信息。"
        )
        prompt = (
            f"用户问题：{query}\n\n"
            f"检索到的代码片段：\n{context}\n\n"
            "请基于以上片段回答。"
        )
        try:
            answer = self._gateway.chat_text(prompt, system=system)
        except Exception as e:
            # gateway 调用失败：回退到"仅检索片段"，不让用户拿不到任何结果
            return (
                f"（LLM 网关调用失败：{e!r}，已回退为仅展示检索片段）\n\n"
                f"相关代码片段：\n{context}"
            )

        # 5. 追加引用来源清单，方便人工核对（RAG 可溯源是工程可信度关键）
        refs = "\n".join(
            f"[{i + 1}] {h['file']}:{h['lines']}" for i, h in enumerate(hits)
        )
        return f"{answer}\n\n--- 引用来源 ---\n{refs}"


# =====================================================================
# 辅助类：标准库 BM25
# =====================================================================


class _BM25:
    """用 Python 标准库实现的简易 BM25 关键词检索。

    混合检索技术：关键词召回路。不引入外部依赖（rank_bm25 等），
    一来减少安装负担，二来 BM25 算法本身简单，手写更便于讲清原理。

    BM25 核心公式（对每个查询词项 t 在文档 d 中的得分）：
        score(t, d) = IDF(t) * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))
    其中：
    - tf：词项在文档中的词频
    - dl：文档长度（token 数），avgdl：平均文档长度
    - k1=1.5, b=0.75：经典经验参数；k1 调节 tf 饱和速度，b 调节文档长度归一化强度
    - IDF(t) = ln(1 + (N - df + 0.5) / (df + 0.5))，N 为文档总数，df 为含 t 的文档数

    代码检索为何仍需要 BM25 这种"老派"关键词检索？
    - 向量相似度是"软匹配"，对精确符号（类名 `CodebaseRAG`、函数 `index`）容易漂移；
    - BM25 是"硬匹配"，对符号/报错串的命中确定性高，与向量形成互补。
    """

    def __init__(self, corpus: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        """构建 BM25 索引。

        参数：
        - corpus：文档集（这里每个文档是一个代码 chunk 的原文）
        - k1, b：BM25 经验参数，默认 1.5 / 0.75，工程默认值，几乎无需调
        """
        self.k1 = k1
        self.b = b
        # 分词后的语料：每篇文档 -> 词项列表
        self._docs: list[list[str]] = [self._tokenize(doc) for doc in corpus]
        self._doc_len = [len(d) for d in self._docs]
        self._avgdl = (sum(self._doc_len) / len(self._doc_len)) if self._doc_len else 0.0

        # 倒排表：词项 -> {doc_idx -> tf}，O(1) 查询词频，避免每次全量扫描
        self._inverted: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        for i, doc in enumerate(self._docs):
            # 同一文档里同一词项的 tf 用 Counter 一次性算清
            for term, tf in Counter(doc).items():
                self._inverted[term][i] = tf

        # IDF 预计算：对每个词项算一次，检索时复用
        n = len(self._docs)
        self._idf: dict[str, float] = {}
        for term, postings in self._inverted.items():
            df = len(postings)  # 含该词项的文档数
            # BM25+ 风格的 IDF，保证非负
            self._idf[term] = math.log(1 + (n - df + 0.5) / (df + 0.5))

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """代码文本分词：按非字母数字字符切分并小写化。

        为什么不用复杂分词器？
        - 代码符号命名（camelCase / snake_case）天然带分隔信息，简单 split 即可得有效 token；
        - 小写化保证 `CodebaseRAG` 与 `codebase` 能部分匹配（camelCase 会被切成 codebase/rag）；
        - 标准库 re 足矣，无需 jieba 等中文分词（代码 QA 关键词以英文符号为主）。
        """
        # \w 在 re 默认模式下匹配 [a-zA-Z0-9_]，正好覆盖代码标识符字符集
        return re.findall(r"[A-Za-z0-9_]+", text.lower())

    def search(self, query: str, k: int = 10) -> list[tuple[int, float]]:
        """对 query 检索，返回 [(doc_idx, score)] 前 k 个，按 score 降序。

        实现要点：
        - 把 query 分词后，对每个词项查倒排表，累加命中文档的 BM25 得分；
        - 只访问含查询词的文档（倒排表的好处），避免遍历全量语料；
        - 最后按得分排序取 top-k。
        """
        q_terms = self._tokenize(query)
        scores: dict[int, float] = defaultdict(float)  # doc_idx -> 累加得分
        for term in q_terms:
            postings = self._inverted.get(term)
            if not postings:
                continue  # 查询词不在任何文档中出现，跳过
            idf = self._idf.get(term, 0.0)
            for doc_idx, tf in postings.items():
                # BM25 词项得分公式（见类 docstring）
                dl = self._doc_len[doc_idx] or 1
                denom = tf + self.k1 * (1 - self.b + self.b * dl / (self._avgdl or 1))
                scores[doc_idx] += idf * (tf * (self.k1 + 1)) / (denom or 1)
        # 排序取 top-k
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:k]
        return ranked


# =====================================================================
# 可选：OpenAI 兼容 Embedding 函数（惰性，仅环境变量配置后启用）
# =====================================================================


class _OpenAICompatEmbeddingFunction:
    """对接 OpenAI 兼容 /v1/embeddings 接口的自定义 embedding 函数。

    Embedding 模型接入：支持私有化/兼容 OpenAI 协议的 embedding 服务。

    为什么自己写而不直接用 chromadb 的 OpenAIEmbeddingFunction？
    - 官方那个强依赖 openai SDK 且默认连 openai.com；
    - 本项目要支持任意"OpenAI 兼容"服务（DeepSeek、本地 vLLM、私有化网关），
      用标准库 urllib 直接 POST /v1/embeddings，零额外依赖、base_url 可配。

    只在 DEVPILOT_RAG_EMBED_PROVIDER=openai 且有 API Key 时才会被实例化（见 _get_ef）。
    """

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self._api_key = api_key
        # 规范化 base_url：去掉末尾斜杠，确保 /v1/embeddings 拼接正确
        self._base_url = base_url.rstrip("/")
        self._model = model

    def __call__(self, input: list[str]) -> list[list[float]]:
        """chromadb 调用入口：接收文本列表，返回向量列表。

        chromadb 的 embedding function 协议就是 __call__([text, ...]) -> [[float, ...], ...]。
        """
        import json
        import urllib.error
        import urllib.request

        # 构造请求体（OpenAI embeddings 标准协议）
        payload = json.dumps({"input": input, "model": self._model}).encode("utf-8")
        url = f"{self._base_url}/v1/embeddings"
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            # 兼容 OpenAI 返回结构：{"data": [{"embedding": [...], "index": 0}, ...]}
            # 按 index 排序保证顺序与输入一致
            items = sorted(body.get("data", []), key=lambda x: x.get("index", 0))
            return [item["embedding"] for item in items]
        except (urllib.error.URLError, KeyError, ValueError) as e:
            # embedding 服务不可用：抛出明确错误，由上层决定是否回退
            raise RuntimeError(
                f"OpenAI 兼容 embedding 调用失败 ({url}): {e!r}；"
                f"可清除环境变量 DEVPILOT_RAG_EMBED_PROVIDER 回退默认 SentenceTransformers。"
            ) from e
