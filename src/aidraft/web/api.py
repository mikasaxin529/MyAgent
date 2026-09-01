"""智绘工坊 AIDraft HTTP API 服务。

把平台能力暴露为 REST 供前端对接。本层只做"协议适配"：不重复实现业务
逻辑，智能体图由 AgentHub 注册中心发现（agenthub/），Skill 由 web.runtime
的 build_registry 装配。

启动：
    pip install -e ".[web]"
    uvicorn aidraft.web.api:app --reload      # 开发：API 在 8000
    # 前端开发服务器（web/frontend）：npm run dev  # 5173，proxy 到 8000

端点契约（前后端共同遵守）：
REST:
    GET  /api/health  → {providers, primary, fallback, chain}
    GET  /api/agents  → {agents:[{agent_id, display_name, ...}]}
    GET  /api/sessions[?agent=]  /api/sessions/{sid}  PUT/DELETE 同路径
    GET  /api/memory/facts → {facts:[...]}
    POST /api/chat    body {prompt, history?, agent?, session_id?}
                      → SSE 流（token/step/outline/review/.../done 帧）
    GET  /files/{agent}/{session}/{path} → 交付物静态文件（防目录穿越）
"""
from __future__ import annotations

# 标准库
import asyncio
import json
import mimetypes
import os
import time
from pathlib import Path
from typing import Any

# 惰性引入 FastAPI：未安装 [web] 时，import 本模块给清晰提示而非裸 ImportError。
try:
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
    from starlette.responses import FileResponse, JSONResponse, StreamingResponse
except ImportError as exc:  # pragma: no cover - 仅缺依赖时触发
    raise ImportError(
        "Web 层依赖未安装，请运行：pip install -e \".[web]\""
    ) from exc

from ..gateway import build_default_gateway


# ----------------------------------------------------------------------
# FastAPI 应用
# ----------------------------------------------------------------------
app = FastAPI(title="智绘工坊 AIDraft API", version="0.1.0")

# CORS：开发期前端跑在 5173，允许跨域。生产同源可关。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# 交付物落盘根目录（契约 3.4）：src/aidraft/web/api.py -> parents[3] = 项目根
# 优先 AIDRAFT_OUTPUTS_DIR（Docker 非 editable 安装时 parents[3] 指向
# site-packages 只读目录，与 AIDRAFT_DIST_DIR 同一套容器约定）。
OUTPUTS_DIR = Path(os.environ.get("AIDRAFT_OUTPUTS_DIR")
                   or Path(__file__).resolve().parents[3] / "outputs")


# ----------------------------------------------------------------------
# REST：健康检查
# ----------------------------------------------------------------------
@app.get("/api/health")
async def health() -> dict:
    """返回网关可用 provider 与路由链，前端据此判断环境是否就绪。"""
    try:
        gw = build_default_gateway()
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "providers": gw.available_providers,
        "primary": gw._primary,
        "fallback": gw._fallback,
        "chain": gw._pick_chain(),
    }


# ----------------------------------------------------------------------
# REST：智能体列表（AgentHub 注册中心）
# ----------------------------------------------------------------------
@app.get("/api/agents")
async def agents() -> dict:
    """列出注册中心发现的所有智能体（前端选择器下拉填充）。"""
    from ..agenthub import list_agents

    return {"agents": [m.to_dict() for m in list_agents()]}


# ----------------------------------------------------------------------
# REST：会话持久化（前端从 localStorage 迁到服务端 SQLite）
# 端点契约（前端 api.ts 同步遵守）：
#     GET    /api/sessions?agent=xxx      → {sessions:[{id,agent_id,title,updated_at},...]}
#     GET    /api/sessions/{sid}          → {session:{id,agent_id,title,messages:[...]}}
#     PUT    /api/sessions/{sid}          body {agent,title,messages} → {ok}
#     DELETE /api/sessions/{sid}          → {ok}
#     GET    /api/memory/facts            → {facts:[...]}（长期记忆，调试/可视化用）
# ----------------------------------------------------------------------
class SessionPut(BaseModel):
    agent: str = "general"
    title: str = "新对话"
    messages: list[dict] = []


@app.get("/api/sessions")
async def sessions(agent: str | None = None) -> dict:
    """列出会话摘要（不含消息体）。agent 参数过滤智能体。"""
    from . import store
    return {"sessions": store.list_sessions(agent)}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str) -> JSONResponse:
    """取整条会话（含消息数组）。404 = 不存在。"""
    from . import store
    sess = store.get_session(session_id)
    if sess is None:
        return JSONResponse(status_code=404, content={"error": "not found"})
    return {"session": sess}


@app.put("/api/sessions/{session_id}")
async def put_session(session_id: str, body: SessionPut) -> dict:
    """整段 upsert 会话（前端流结束时一次性落全量消息）。"""
    from . import store
    store.upsert_session(session_id, body.agent, body.title, body.messages)
    return {"ok": True}


@app.delete("/api/sessions/{session_id}")
async def del_session(session_id: str) -> dict:
    """删除会话（消息级联删除，文件产物不删）。"""
    from . import store
    return {"ok": store.delete_session(session_id)}


@app.get("/api/memory/facts")
async def memory_facts(limit: int = 30) -> dict:
    """长期记忆：最近 N 条用户事实（跨会话共享）。"""
    from . import store
    return {"facts": store.recent_facts(limit)}


# ----------------------------------------------------------------------
# SSE：ChatFlow 式流式聊天（POST /api/chat → text/event-stream）
# ----------------------------------------------------------------------
def _sse(obj: dict) -> str:
    """把一帧序列化为 SSE 行：data: {json}\n\n。"""
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# 通用对话图节点 → 步骤显示名映射（时间线用）
_GENERAL_STEP_LABELS: dict[str, str] = {
    "route_model": "路由判断",
    "planner": "规划步骤",
    "call_model": "模型推理",
    "tools": "工具调用",
    "call_model_after_tool": "综合推理",
    "reflector": "反思",
    "save_response": "保存回答",
    "extract_memory": "提取记忆",
    "compress_memory": "压缩记忆",
}


def _wrap_emitter_for_steps(
    inner_emit: Any,
    step_labels: dict[str, str],
) -> Any:
    """包装 emitter，在原有 node 帧旁同时发射 step 帧（时间线用）。

    node 帧（type="node"）由 cf/base.py 的 visit()/done() 发出；
    本包装将其转换为 step 帧（type="step"），供右侧 Timeline 组件渲染。
    """
    def _emit(frame: dict) -> None:
        t = frame.get("type")
        if t == "node":
            node_id = frame.get("node_id", "")
            status = frame.get("status", "")
            label = step_labels.get(node_id, node_id)
            step_frame: dict = {
                "type": "step",
                "id": node_id,
                "label": label,
                "status": status,
                "ts": time.time(),
            }
            inner_emit(step_frame)
        inner_emit(frame)
    return _emit


@app.post("/api/chat")
async def chat_sse(request: Request) -> StreamingResponse:
    """SSE 流式聊天：POST {prompt, history?, agent?}，返 text/event-stream。

    支持多智能体路由：agent 字段指定智能体 id（默认 "general"）。
    通用对话走现有 ChatFlow 图（build_chat_graph），
    其他智能体走其 graph.py 导出的 build_graph()。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    prompt = (body.get("prompt") or "").strip() if isinstance(body, dict) else ""
    history = body.get("history", []) if isinstance(body, dict) else []
    agent_id = body.get("agent", "general") if isinstance(body, dict) else "general"
    session_id = (body.get("session_id") or "").strip() if isinstance(body, dict) else ""
    if not prompt:
        async def _err():
            yield _sse({"type": "error", "message": "empty prompt"})
        return StreamingResponse(_err(), media_type="text/event-stream")

    # 1) 查注册中心获取智能体
    from ..agenthub import get_agent
    agent = get_agent(agent_id)
    if agent is None or agent.graph_fn is None:
        async def _err():
            yield _sse({"type": "error", "message": f"unknown agent: {agent_id}"})
        return StreamingResponse(_err(), media_type="text/event-stream")

    from ..gateway import build_default_gateway
    from .runtime import build_registry
    from ..governance.audit import AuditLog
    from ..runtime import Memory
    from ..graph.nodes import SYSTEM_CHAT

    event_q: asyncio.Queue = asyncio.Queue()

    def _emit(frame: dict) -> None:
        try:
            event_q.put_nowait(frame)
        except Exception:  # noqa: BLE001
            pass

    # 2) 构造运行时依赖
    gw = build_default_gateway()
    registry = build_registry()
    audit = AuditLog()

    # 3) 构建智能体图（注入 emitter，节点产出帧投 event_q）
    # 通用对话图额外包装 step 帧发射（时间线）
    if agent_id == "general":
        inner_emit = _wrap_emitter_for_steps(_emit, _GENERAL_STEP_LABELS)
    else:
        inner_emit = _emit

    graph = agent.graph_fn(
        gateway=gw, registry=registry, audit=audit, emitter=inner_emit,
    )

    # 4) 历史压缩：Memory 三段式摘要
    mem = Memory(max_tokens=8000)
    mem.set_summarizer(gw)
    for m in history:
        if isinstance(m, dict):
            mem.add(m.get("role", "user"), str(m.get("content", "")))
    compressed = mem.to_messages()
    if any(
        isinstance(m, dict) and "[历史摘要]" in m.get("content", "")
        for m in compressed
    ):
        _emit({"type": "thinking", "node": "system", "phase": "content",
               "delta": "[系统] 已将早期对话压缩为摘要，保留最近 6 条原文。\n"})
    messages_for_graph = compressed + [{"role": "user", "content": prompt}]
    # 长期记忆注入：把跨会话用户事实附到 system 消息（业内 Letta/Mem0 范式：
    # 长期记忆按相关性/时间取最近 N 条，拼进 system 上下文）。失败静默——
    # 记忆层不可用不应阻断聊天主链路。
    try:
        from . import store as _store
        facts = [f["fact"] for f in _store.recent_facts(15)]
    except Exception:  # noqa: BLE001
        facts = []
    # 仅 managed_system=True 的智能体（general）由端点注入 SYSTEM_CHAT。
    # 其他智能体（如 yuwen）由图自管 system 消息，避免双 system 冲突。
    if agent.managed_system:
        sys_text = SYSTEM_CHAT
        if facts:
            sys_text += "\n\n[用户长期记忆]\n" + "\n".join(f"- {f}" for f in facts)
            _emit({"type": "thinking", "node": "system", "phase": "content",
                   "delta": f"[系统] 已注入 {len(facts)} 条长期记忆。\n"})
        messages_for_graph.insert(0, {"role": "system", "content": sys_text})
    elif facts and messages_for_graph and isinstance(messages_for_graph[0], dict) \
            and messages_for_graph[0].get("role") == "system":
        # 图自管 system 的智能体：直接在原 system 后追加，不额外插一条。
        messages_for_graph[0] = {
            **messages_for_graph[0],
            "content": messages_for_graph[0]["content"]
            + "\n\n[用户长期记忆]\n" + "\n".join(f"- {f}" for f in facts),
        }

    state_final = {"fa": ""}

    # 5) 先发 agent_meta 帧（智能体信息，前端渲染顶栏）
    _emit({
        "type": "agent_meta",
        "agent_id": agent.agent_id,
        "display_name": agent.display_name,
        "description": agent.description,
        "identity_color": agent.identity_color,
        "placeholder": agent.placeholder,
    })

    async def _run_graph() -> None:
        try:
            async for chunk in graph.astream(
                {"task": prompt, "user_message": prompt,
                 "messages": messages_for_graph, "session_id": session_id}
            ):
                for _node_id, update in chunk.items():
                    if isinstance(update, dict) and update.get("final_answer"):
                        state_final["fa"] = update["final_answer"]
        except Exception as exc:  # noqa: BLE001
            _emit({"type": "error", "message": repr(exc)})
        finally:
            _emit({"type": "_graph_done"})

    task = asyncio.create_task(_run_graph())

    async def event_stream():
        final_answer = ""
        nodes_visited: list[str] = []
        saw_done = False
        while True:
            try:
                frame = await asyncio.wait_for(event_q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                if task.done():
                    break
                continue
            t = frame.get("type")
            if t == "_graph_done":
                break
            if t == "error":
                yield _sse(frame)
                break
            # 图节点已发 done 帧（如 yuwen report）时，不再追加终帧，避免双 done
            if t == "done":
                saw_done = True
                yield _sse(frame)
                continue
            # final_answer 累加：同时覆盖 content 与 token 两种正文帧
            if t in ("content", "token"):
                final_answer += frame.get("delta", "")
            # 追踪已访问节点（step 帧 running 时记录）
            if t == "step" and frame.get("status") == "running":
                nid = frame.get("id", "")
                if nid and nid not in nodes_visited:
                    nodes_visited.append(nid)
            yield _sse(frame)
        # 终帧 done：图节点未发 done 时由本端点兜底。优先用 state 的
        # final_answer（图节点如 report 或 save_response 已提供干净摘要），
        # 其次回退到累加的 token 正文（direct_chat 等不设 final_answer 的路径）。
        if saw_done:
            return
        if state_final["fa"]:
            final_answer = state_final["fa"]
        yield _sse({
            "type": "done",
            "answer": final_answer,
            "meta": {
                "nodes_visited": nodes_visited,
                "audit_total": len(audit.entries()) if hasattr(audit, "entries") else 0,
            },
        })

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ----------------------------------------------------------------------
# 文件服务：交付物静态文件下载（防目录穿越）
# 必须在 SPA 兜底路由之前注册，防止 /files/... 被 {full_path:path} 捕获。
# ----------------------------------------------------------------------
@app.get("/files/{agent_id}/{session}/{filename:path}")
async def serve_file(agent_id: str, session: str, filename: str, inline: int = 0) -> Any:
    """交付物静态文件下载。

    默认发 attachment（浏览器强制下载，filename 参数保证中文名不乱码）；
    带 ?inline=1 时不发 Content-Disposition，供 HTML 课件浏览器内预览。

    安全要点：
    - 用 Path.resolve() 解析完整路径后断言 is_relative_to(OUTPUTS_DIR)
    - 仅允许白名单根目录（OUTPUTS_DIR）下的文件
    - filename 经 URL 解码后参与路径拼接
    """
    from urllib.parse import unquote

    safe_agent = Path(agent_id).as_posix()
    safe_session = Path(session).as_posix()
    safe_file = Path(unquote(filename)).as_posix()
    # 拒绝任何含路径分隔符的段（防 agent_id 穿越）
    if "/" in safe_agent or "\\" in safe_agent:
        return JSONResponse(status_code=400, content={"error": "invalid agent_id"})
    requested = (OUTPUTS_DIR / safe_agent / safe_session / safe_file).resolve()
    try:
        requested.relative_to(OUTPUTS_DIR.resolve())
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "path traversal denied"})
    if not requested.is_file():
        return JSONResponse(status_code=404, content={"error": "file not found"})
    mime_type, _ = mimetypes.guess_type(str(requested))
    media = mime_type or "application/octet-stream"
    if inline:
        return FileResponse(str(requested), media_type=media)
    # filename= 让 Starlette 发 Content-Disposition: attachment; filename*=utf-8''…
    return FileResponse(str(requested), media_type=media, filename=requested.name)


# ----------------------------------------------------------------------
# 可选：托管前端构建产物（生产单命令部署）
# 设计：API 路由已在上方注册（优先匹配）；此处只兜底静态资源与
# SPA 客户端路由。/assets/* 走 StaticFiles（JS/CSS chunk），其余未匹配路径
# 回退 index.html，交 react-router 客户端解析。
# ----------------------------------------------------------------------
# 优先取 AIDRAFT_DIST_DIR 环境变量（Docker 里 pip install 非 editable 安装时，
# __file__ 位于 site-packages，仓库相对路径推断会指向不存在的位置）。
_dist = Path(os.environ.get("AIDRAFT_DIST_DIR")
             or Path(__file__).resolve().parents[3] / "web" / "frontend" / "dist")
if _dist.is_dir():
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    _index = _dist / "index.html"
    # /assets 下是 Vite 打包出的带 hash 的 JS/CSS，直接当静态文件发。
    app.mount("/assets", StaticFiles(directory=str(_dist / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def _spa(full_path: str) -> Any:
        """SPA 兜底：未匹配 API/assets 的路径一律回 index.html，交客户端路由。"""
        return FileResponse(str(_index))

