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
import queue as _queue
from dataclasses import asdict
from pathlib import Path
from typing import Any

# 惰性引入 FastAPI：未安装 [web] 时，import 本模块给清晰提示而非裸 ImportError。
try:
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
    from starlette.responses import StreamingResponse
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
# Chat graph 帧协议（/ws/chat）
# ----------------------------------------------------------------------
# ChatGPT 式流式聊天的 WS 帧类型，与前端 FlowGraph + Chat 页共同遵守：
#   {type:"route",   route, reason}        — router 节点判出的路由 + 理由
#   {type:"reasoning", delta}              — 推理模型的思考过程增量（reasoning_content）
#   {type:"token",    delta}               — 正文 token 增量（逐字渲染）
#   {type:"node",     node_id, status}     — 节点 running/done（图形流高亮）
#   {type:"blackboard", data}              — dev 分支黑板快照（plan/code_diff/...）
#   {type:"step",     step}               — 中间步骤（搜索词/搜索结果等，进思考区）
#   {type:"done",     answer, meta}        — 终帧
#   {type:"error",    message}             — 错误
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# 请求/响应模型（Pydantic）
# ----------------------------------------------------------------------
class ChatRequest(BaseModel):
    prompt: str


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


# ----------------------------------------------------------------------
# WebSocket：ChatGPT 式流式聊天（驱动 langgraph 编排图）
# ----------------------------------------------------------------------
@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket) -> None:
    """流式聊天：接收 {prompt}，驱动 langgraph 图，逐帧推送。

    与 /ws/run 的区别：
    - /ws/run：跑原 orchestrator 顺序流水线（plan→coder→review→test）+ 审批握手，
      后台线程执行（同步 orch.run）。
    - /ws/chat：跑 langgraph 编排图（router→chat/websearch/dev 分支），全 async，
      emitter 把 token/reasoning/node/route/blackboard/step 帧投到 event_q，
      本处理函数 async for 图状态更新同时 drain event_q 推前端。

    设计：
    - graph 节点内调 gateway.stream_chat 逐 token yield，经 emitter 投 event_q。
    - graph.astream 在事件循环里 async 迭代（不阻塞），每个状态更新也投 event_q
      作 blackboard 快照（dev 分支有用）。
    - 用 asyncio.create_task 跑图的 astream 消费循环，主循环同时 drain 推 WS，
      实现"图产出事件"与"WS 推送"的并发。
    """
    await websocket.accept()
    try:
        # 1) 等客户端发 {prompt, history?}（history 为连续对话历史，首轮为空）
        raw = await websocket.receive_text()
        try:
            req = json.loads(raw)
        except Exception:
            req = {}
        prompt = req.get("prompt", "")
        history = req.get("history", []) or []
        if not prompt:
            await websocket.send_json({"type": "error", "message": "empty prompt"})
            return

        # 2) 装配 chat graph 运行时
        from ..graph import build_chat_graph_runtime
        event_q: asyncio.Queue = asyncio.Queue()
        # chat 路径默认用普通 ApprovalGate（非交互环境默认拒绝高危动作，
        # 保证安全）。如需 web 审批握手，可在此注入 WebApprovalGate + 队列握手。
        gw, registry, audit, approval, graph, set_emitter = build_chat_graph_runtime()
        # 注入 emitter：节点产出的帧投到 event_q（async queue，线程安全）。
        # 注意：graph 节点是 async 的，跑在事件循环线程，emitter 也在同线程调用。
        def _emit(frame: dict) -> None:
            try:
                event_q.put_nowait(frame)
            except Exception:  # noqa: BLE001
                pass
        set_emitter(_emit)

        # 2.5) 历史压缩：用 Memory 把前端发来的对话历史压缩（超 token 预算三段式
        # 摘要：首 system + 最近 6 条原文 + 中间 LLM 摘要），拼成
        # [system, ...历史, user(当前prompt)] 多轮 messages 喂 graph，实现连续对话。
        from ..runtime import Memory
        from ..graph.nodes import SYSTEM_CHAT
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
            _emit({"type": "reasoning",
                   "delta": "[系统] 已将早期对话压缩为摘要，保留最近 6 条原文。\n"})
        messages_for_graph = (
            [{"role": "system", "content": SYSTEM_CHAT}]
            + compressed
            + [{"role": "user", "content": prompt}]
        )

        # 3) 后台任务：驱动 graph.astream。动态图的关键帧（plan/node/token/
        # step/reasoning）已由各节点的 emitter 直接投 event_q；这里再把每步
        # 状态更新里的 step_results 快照投一份，供前端思考区展示中间产出。
        # 同时追踪 state 里的 final_answer：天气/websearch 等不调 LLM 的分支
        # 不产 token 帧，done.answer 需从 state 兜底（direct/planner 路径
        # 已有 token 流累加，优先用 token）。
        state_final = {"fa": ""}
        async def _run_graph() -> None:
            try:
                async for chunk in graph.astream({"task": prompt, "messages": messages_for_graph}):
                    for node_id, update in chunk.items():
                        if not isinstance(update, dict):
                            continue
                        # 动态编排：把每步产出作快照投递（前端思考区/黑板区展示）。
                        if "step_results" in update:
                            _emit({"type": "blackboard", "data": {
                                "step_results": update["step_results"],
                                "step_index": update.get("step_index", 0),
                            }})
                        if "plan_steps" in update:
                            _emit({"type": "blackboard", "data": {
                                "plan_steps": update["plan_steps"],
                            }})
                        if update.get("final_answer"):
                            state_final["fa"] = update["final_answer"]
            except Exception as exc:  # noqa: BLE001
                _emit({"type": "error", "message": repr(exc)})
            finally:
                _emit({"type": "_graph_done"})

        task = asyncio.create_task(_run_graph())

        # 4) 主循环：drain event_q 推 WS，直到图跑完。
        final_answer = ""
        while True:
            try:
                frame = await asyncio.wait_for(event_q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                if task.done():
                    break
                continue
            # 内部哨兵：图跑完。
            if frame.get("type") == "_graph_done":
                break
            if frame.get("type") == "error":
                await websocket.send_json(frame)
                break
            # token 帧累加 final_answer（供 done 帧返回）。
            if frame.get("type") == "token":
                final_answer += frame.get("delta", "")
            await websocket.send_json(frame)

        # 5) 取图最终状态（final_answer 在 state 里）。
        try:
            final_state = await task
        except Exception as exc:  # noqa: BLE001
            await websocket.send_json({"type": "error", "message": repr(exc)})
            return

        # token 流优先（direct/planner 路径逐字累加）；为空时用 state 的
        # final_answer 兜底（天气/websearch 等不调 LLM 的分支）。
        if not final_answer and state_final["fa"]:
            final_answer = state_final["fa"]

        # 6) 终帧 done。
        await websocket.send_json({
            "type": "done",
            "answer": final_answer,
            "meta": {
                "nodes_visited": [],
                "audit_total": len(audit.entries()) if hasattr(audit, "entries") else 0,
            },
        })

    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001
        try:
            await websocket.send_json({"type": "error", "message": repr(exc)})
        except Exception:  # noqa: BLE001
            pass


# ----------------------------------------------------------------------
# SSE：ChatFlow 式流式聊天（POST /api/chat → text/event-stream）
# ----------------------------------------------------------------------
def _sse(obj: dict) -> str:
    """把一帧序列化为 SSE 行：data: {json}\n\n。"""
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.post("/api/chat")
async def chat_sse(request: Request) -> StreamingResponse:
    """SSE 流式聊天：POST {prompt, history?}，返 text/event-stream。

    与 /ws/chat 的区别：用 SSE（fetch+ReadableStream）替代 WS，推 ChatFlow 式
    细粒度帧（thinking/content/route/plan/tool_call/tool_call_start/tool_call_args/
    tool_result/search_item/reflection/status/memory/node/done/error）。前端 processLine
    按 type dispatch 到 onXxx 回调，渲染 think-block/tool-block-sources/ai-content。

    历史压缩、graph 装配复用 /ws/chat 同款逻辑（build_chat_graph_runtime + Memory）。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    prompt = (body.get("prompt") or "").strip() if isinstance(body, dict) else ""
    history = body.get("history", []) if isinstance(body, dict) else []
    if not prompt:
        async def _err():
            yield _sse({"type": "error", "message": "empty prompt"})
        return StreamingResponse(_err(), media_type="text/event-stream")

    from ..graph import build_chat_graph_runtime
    from ..runtime import Memory
    from ..graph.nodes import SYSTEM_CHAT

    event_q: asyncio.Queue = asyncio.Queue()
    gw, registry, audit, _approval, graph, set_emitter = build_chat_graph_runtime()

    def _emit(frame: dict) -> None:
        try:
            event_q.put_nowait(frame)
        except Exception:  # noqa: BLE001
            pass

    set_emitter(_emit)

    # 历史压缩：复用 /ws/chat 的 Memory 三段式摘要。
    mem = Memory(max_tokens=8000)
    mem.set_summarizer(gw)
    for m in history:
        if isinstance(m, dict):
            mem.add(m.get("role", "user"), str(m.get("content", "")))
    compressed = mem.to_messages()
    if any(isinstance(m, dict) and "[历史摘要]" in m.get("content", "") for m in compressed):
        _emit({"type": "thinking", "node": "system", "phase": "content",
               "delta": "[系统] 已将早期对话压缩为摘要，保留最近 6 条原文。\n"})
    messages_for_graph = (
        [{"role": "system", "content": SYSTEM_CHAT}]
        + compressed
        + [{"role": "user", "content": prompt}]
    )

    state_final = {"fa": ""}

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
            if t == "content":
                final_answer += frame.get("delta", "")
            yield _sse(frame)
        # 终帧 done。
        if not final_answer and state_final["fa"]:
            final_answer = state_final["fa"]
        yield _sse({
            "type": "done",
            "answer": final_answer,
            "meta": {
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

