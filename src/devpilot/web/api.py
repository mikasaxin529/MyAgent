"""DevPilot HTTP API + WebSocket 服务。

把现有 CLI 能力暴露为 REST + WS，供前端对接。本层只做"协议适配"：
不重复实现业务逻辑，全部复用 gateway / SkillRegistry / Orchestrator /
run_evaluation。CLI 与 API 行为一致，单一事实源在 web.runtime.build_runtime。

启动：
    pip install -e ".[web]"
    uvicorn devpilot.web.api:app --reload      # 开发：API 在 8000
    # 前端开发服务器（web/frontend）：npm run dev  # 5173，proxy 到 8000

端点契约（前后端共同遵守）：
REST:
    GET  /api/health  → {providers, primary, fallback, chain}
    POST /api/chat    body {prompt} → {content, provider, model, latency_ms, ...}
    GET  /api/skills  → [{name, specs:[{name, description, schema}]}, ...]
    POST /api/eval    → Metrics.to_dict() + per_tag
WS:
    /ws/run  客户端先发 {"task":"..."}；服务端流式推送 audit / blackboard /
             approval_request 帧；遇审批需客户端回 {"decision","comment","args"}；
             最终发 {"type":"done", blackboard, summary}。
"""
from __future__ import annotations

# 标准库
import asyncio
import json
import mimetypes
import queue as _queue
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

# 惰性引入 FastAPI：未安装 [web] 时，import 本模块给清晰提示而非裸 ImportError。
try:
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
    from starlette.responses import FileResponse, JSONResponse, StreamingResponse
except ImportError as exc:  # pragma: no cover - 仅缺依赖时触发
    raise ImportError(
        "Web 层依赖未安装，请运行：pip install -e \".[web]\""
    ) from exc

from ..gateway import build_default_gateway, ChatMessage
from ..governance.approval import ApprovalResult
from ..governance.audit import AuditEntry
from .events import ObservableAuditLog
from .approval_web import WebApprovalGate
from .runtime import build_runtime, build_registry


# ----------------------------------------------------------------------
# 请求/响应模型（Pydantic）
# ----------------------------------------------------------------------
class ChatRequest(BaseModel):
    prompt: str
    history: list[dict] = []
    agent: str = "general"


# ----------------------------------------------------------------------
# FastAPI 应用
# ----------------------------------------------------------------------
app = FastAPI(title="DevPilot API", version="0.1.0")

# CORS：开发期前端跑在 5173，允许跨域。生产同源可关。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# 交付物落盘根目录（契约 3.4）：src/devpilot/web/api.py -> parents[3] = 项目根
OUTPUTS_DIR = Path(__file__).resolve().parents[3] / "outputs"


def _eval_path(rel: str) -> str:
    """定位项目根下的资源文件（如 eval_data/golden.jsonl）。

    本文件在 src/devpilot/web/api.py，项目根 = parents[3]。
    """
    here = Path(__file__).resolve()
    root = here.parents[3]  # web/api.py -> devpilot -> src -> 项目根
    return str(root / rel)


def _entry_dict(e: AuditEntry) -> dict:
    """审计条目转可序列化 dict。"""
    return asdict(e)


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
# REST：单轮对话
# ----------------------------------------------------------------------
@app.post("/api/chat/legacy")
async def chat(req: ChatRequest) -> dict:
    """单轮对话（REST，非流式）：经网关调一轮 LLM，返回正文与 meta。

    注：流式聊天走 POST /api/chat（SSE）。本端点保留供 CLI/eval 单轮直调。"""
    gw = build_default_gateway()
    resp = gw.chat(
        [
            ChatMessage("system", "你是 DevPilot 的助手，简洁专业地回答。"),
            ChatMessage("user", req.prompt),
        ]
    )
    return {
        "content": resp.content,
        "provider": resp.provider,
        "model": resp.model,
        "latency_ms": resp.latency_ms,
        "prompt_tokens": resp.prompt_tokens,
        "completion_tokens": resp.completion_tokens,
    }


# ----------------------------------------------------------------------
# REST：Skill 生态
# ----------------------------------------------------------------------
@app.get("/api/skills")
async def skills() -> list[dict]:
    """列出已注册 Skill 及其能力清单（按 Skill 分组）。"""
    registry = build_registry()
    result: list[dict] = []
    for name in registry.list_skills():
        skill = registry.get(name)
        specs = [
            {"name": s.name, "description": s.description, "schema": s.schema}
            for s in skill.specs()
        ]
        result.append({"name": name, "specs": specs})
    return result


# ----------------------------------------------------------------------
# REST：评估体系
# ----------------------------------------------------------------------
@app.post("/api/eval")
async def eval_run() -> dict:
    """跑 Evaluation Harness，返回多维度指标。

    复用 CLI cmd_eval 的逻辑：加载 golden.jsonl → LLMJudge → stub
    agent_run_fn → run_evaluation。真实场景 agent_run_fn 应跑 Orchestrator。
    """
    from ..eval.dataset import GoldenSet
    from ..eval.judge import LLMJudge
    from ..eval.metrics import run_evaluation
    import time

    golden_set = GoldenSet()
    golden_set.load_jsonl(_eval_path("eval_data/golden.jsonl"))
    gw = build_default_gateway()
    judge = LLMJudge(gw)

    # stub 被测 Agent：签名 callable(task)->(output, latency_ms, tokens)。
    def agent_run_fn(task: str):
        t0 = time.time()
        output = gw.chat_text(task, system="你是研发助手，针对需求给出简短的改动方案。")
        return output, (time.time() - t0) * 1000.0, 0

    metrics = run_evaluation(golden_set, judge, agent_run_fn)
    return metrics.to_dict()


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
    # 仅 managed_system=True 的智能体（general）由端点注入 SYSTEM_CHAT。
    # 其他智能体（如 yuwen_skill）由图自管 system 消息，避免双 system 冲突。
    if agent.managed_system:
        messages_for_graph.insert(0, {"role": "system", "content": SYSTEM_CHAT})

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
                {"task": prompt, "user_message": prompt, "messages": messages_for_graph}
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
# WebSocket：实时 Multi-Agent 流程
# ----------------------------------------------------------------------
@app.websocket("/ws/run")
async def ws_run(websocket: WebSocket) -> None:
    """实时跑 Orchestrator 并流式推送事件。

    帧协议见模块 docstring。核心设计：
      - Orchestrator 跑在后台线程（run_in_executor），不阻塞事件循环；
      - ObservableAuditLog 把每条 audit 事件投递到 event_q（线程安全队列），
        主循环 drain 后 send 给前端；
      - Orchestrator.emitter 把每个 worker 后的 Blackboard 快照投递到 event_q；
      - WebApprovalGate 把审批请求投递到 req_q，主循环取到后推前端弹框，
        前端回填 decision 后放 res_q，gate 解阻塞继续。
    """
    await websocket.accept()
    try:
        # 1) 等客户端发 task
        raw = await websocket.receive_text()
        task = json.loads(raw).get("task", "")
        if not task:
            await websocket.send_json({"type": "error", "message": "empty task"})
            return

        # 2) 装配可观测审计 + Web 审批门
        event_q: "_queue.Queue[dict]" = _queue.Queue()  # 待推送帧
        req_q: "_queue.Queue" = _queue.Queue()          # 审批请求
        res_q: "_queue.Queue" = _queue.Queue()          # 审批裁决

        audit = ObservableAuditLog()
        # 订阅者：把 audit 事件包装成帧投递 event_q（在 Worker 线程被调用，queue 线程安全）。
        audit.subscribe(lambda e: event_q.put({"type": "audit", "entry": _entry_dict(e)}))

        approval = WebApprovalGate(req_q, res_q)

        # 3) 装配 Orchestrator（注入可观测审计 + Web 审批门）
        gw, registry, _, _, orch = build_runtime(audit=audit, approval=approval)
        # 注入 Blackboard 快照回调：每个 worker 后推送黑板状态。
        orch.emitter = lambda bb: event_q.put({"type": "blackboard", "data": bb})

        # 4) 后台线程跑 orchestrator.run（阻塞调用，必须脱离事件循环）
        loop = asyncio.get_event_loop()
        future = loop.run_in_executor(None, orch.run, task)

        # 5) 主循环：drain 事件 + 处理审批，直到 Orchestrator 完成
        while True:
            done = future.done()

            # 5.1 先把已产生的事件全部推出去（非阻塞 drain）
            drained = False
            while True:
                try:
                    frame = event_q.get_nowait()
                except _queue.Empty:
                    break
                drained = True
                await websocket.send_json(frame)

            # 5.2 检查是否有审批请求需要前端裁决
            try:
                req = req_q.get_nowait()
            except _queue.Empty:
                req = None

            if req is not None:
                # 推审批请求给前端
                await websocket.send_json({
                    "type": "approval_request",
                    "action": req.action,
                    "args": req.args,
                    "reason": req.reason,
                })
                # 阻塞等前端回填 decision（此时 Worker 线程正阻塞在 res_q.get）
                try:
                    dec_raw = await websocket.receive_text()
                except WebSocketDisconnect:
                    # 客户端断连：放一个拒绝解阻塞 Worker，安全退出
                    res_q.put(ApprovalResult(approved=False, comment="客户端断连"))
                    raise
                dec = json.loads(dec_raw)
                decision = dec.get("decision")
                comment = dec.get("comment", "")
                if decision == "approve":
                    res_q.put(ApprovalResult(approved=True, comment=comment))
                elif decision == "edit":
                    res_q.put(ApprovalResult(
                        approved=True, comment=comment,
                        modified_args=dec.get("args"),
                    ))
                else:  # reject 或未知
                    res_q.put(ApprovalResult(approved=False, comment=comment or "人工拒绝"))

            # 5.3 完成 且 本轮无新事件无审批 → 收尾
            if done and not drained and req is None:
                break
            # 否则小憩继续轮询（审批期间 Worker 在跑，会持续产事件）
            await asyncio.sleep(0.05)

        # 6) 取结果（异常会在此抛出）
        try:
            bb = await asyncio.wrap_future(future)
        except Exception as exc:  # noqa: BLE001
            await websocket.send_json({"type": "error", "message": repr(exc)})
            return

        # 7) 发终帧 done（完整黑板 + 审计摘要）
        # artifacts 里可能含 ApprovalResult（dataclass）——asdict 已递归转 dict。
        await websocket.send_json({
            "type": "done",
            "blackboard": asdict(bb),
            "summary": {
                "total_events": len(audit.entries()),
                "by_event": audit.to_summary(),
            },
        })

    except WebSocketDisconnect:
        # 客户端正常/异常断连：静默退出，不记为服务端错误。
        return
    except Exception as exc:  # noqa: BLE001 - 兜底，避免 WS 处理器抛未捕获异常
        try:
            await websocket.send_json({"type": "error", "message": repr(exc)})
        except Exception:  # noqa: BLE001
            pass


# ----------------------------------------------------------------------
# 文件服务：交付物静态文件下载（防目录穿越）
# 必须在 SPA 兜底路由之前注册，防止 /files/... 被 {full_path:path} 捕获。
# ----------------------------------------------------------------------
@app.get("/files/{agent_id}/{session}/{filename:path}")
async def serve_file(agent_id: str, session: str, filename: str) -> Any:
    """交付物静态文件下载。

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
    return FileResponse(str(requested), media_type=mime_type or "application/octet-stream")


# ----------------------------------------------------------------------
# 可选：托管前端构建产物（生产单命令部署）
# 设计：API 路由与 /ws 已在上方注册（优先匹配）；此处只兜底静态资源与
# SPA 客户端路由。/assets/* 走 StaticFiles（JS/CSS chunk），其余未匹配路径
# 回退 index.html，让 react-router 在客户端解析 /run、/eval 等深链。
# ----------------------------------------------------------------------
_dist = Path(__file__).resolve().parents[3] / "web" / "frontend" / "dist"
if _dist.is_dir():
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    _index = _dist / "index.html"
    # /assets 下是 Vite 打包出的带 hash 的 JS/CSS，直接当静态文件发。
    app.mount("/assets", StaticFiles(directory=str(_dist / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def _spa(full_path: str) -> Any:
        """SPA 兜底：未匹配 API/WS/assets 的路径一律回 index.html，交客户端路由。"""
        return FileResponse(str(_index))

