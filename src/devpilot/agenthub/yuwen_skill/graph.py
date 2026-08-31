"""语文智能体图（contract §5）：extract_params → gen_content → render → report。

节点链：
  extract_params（对话追问收集参数，条件边双出口）
  → gen_content（LLM 按 references/schema.md 生 JSON，自检+重试）
  → render（调 render_all.py 产出 pptx/html/docx）
  → report（汇总文件清单，推 files/done 帧）

条件边：_params_ready 读 state["yuwen_params_ready"]。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from ...gateway import ChatMessage

# ---------------------------------------------------------------------------
# 状态类型
# ---------------------------------------------------------------------------

class YuwenState(TypedDict, total=False):
    """语文智能体图状态（增量合并，只返回自己改的字段）。"""
    # 通用（与 /api/chat 兼容）
    task: str
    user_message: str
    messages: list
    final_answer: str
    nodes_visited: list
    # 语文专用
    yuwen_params: dict           # {title, grade, lesson_type, textbook}
    yuwen_params_ready: bool     # True=齐备放行 / False=追问后 END
    yuwen_content_path: str      # 临时 JSON 文件路径
    yuwen_content: dict          # 课程 JSON 内容
    yuwen_render_error: str      # 渲染错误标记
    yuwen_error: str             # 内容生成错误标记（gen_content 失败时写入，render/report 透传优先展示）
    yuwen_files: list            # [{name, path, size, mime}, ...]


# ---------------------------------------------------------------------------
# 路径定位
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _THIS_DIR / "scripts"
_REFERENCES_DIR = _THIS_DIR / "references"
_PROJECT_ROOT = _THIS_DIR.parents[3]  # yuwen_skill → agenthub → devpilot → src → 项目根
_OUTPUTS_DIR = _PROJECT_ROOT / "outputs"


def _session_name(params: dict) -> str:
    """从参数生成会话目录名（安全文件名）。"""
    title = params.get("title", "untitled")
    lesson_type = params.get("lesson_type", "unknown")
    safe_title = "".join(c for c in title if c not in '\\/:*?"<>|')
    safe_lt = "".join(c for c in lesson_type if c not in '\\/:*?"<>|')
    return f"{safe_title}-{safe_lt}"


# ---------------------------------------------------------------------------
# Emitter 辅助
# ---------------------------------------------------------------------------

def _emit(emitter: Callable[[dict], None] | None, frame: dict) -> None:
    if emitter is None:
        return
    try:
        emitter(frame)
    except Exception:
        pass


def _step(emitter: Callable[[dict], None] | None,
          step_id: str, label: str, status: str, detail: str = "") -> None:
    _emit(emitter, {
        "type": "step",
        "id": step_id,
        "label": label,
        "status": status,
        "ts": time.time(),
        "detail": detail,
    })


def _read_ref(file_name: str) -> str:
    """读取 references/ 下的参考文件内容。"""
    path = _REFERENCES_DIR / file_name
    if path.exists():
        return path.read_text(encoding="utf-8")
    return f"（{file_name} 未找到）"


# ---------------------------------------------------------------------------
# 节点：extract_params
# ---------------------------------------------------------------------------

SYSTEM_EXTRACT = """你是一个语文课件参数提取助手。你需要从用户消息中提取以下三个参数：

1. 课文名（title）：如"静夜思"、"坐井观天"
2. 年级（grade）：1-6 的整数
3. 课型（lesson_type）："精读" / "识字写字" / "古诗词" / "口语交际习作"之一

如果用户只给了课文名但没有给年级，默认课型为"精读"，但需要确认。
如果用户只给了课文名，默认年级为 2，但需要确认。
如果用户什么都没给，需要询问。

以 JSON 格式返回，格式：
{
  "title": "课文名或空串",
  "grade": 年级数字或0,
  "lesson_type": "课型或空串",
  "textbook": "教材版本（LLM 推断，如"部编版二年级上册"）",
  "params_ready": true或false,
  "question": "向用户提问的内容（params_ready=false 时必填，否则填空串）",
  "chips": ["可选项1", "可选项2"]  (params_ready=false 时给用户快捷选择)
}

注意：
- "精读"课型如果用户没有指定课时数，默认 2 课时
- 年级必须是 1-6 的整数
- 课型必须是四种之一
- 如果用户消息中有明显的课文名，优先提取
- 教材版本由 LLM 根据课文名和年级推断"""


def _normalize_grade(raw: Any) -> int:
    """把 LLM 返回的年级归一化为 int。

    DeepSeek/Qwen json_mode 常返回 2.0(float) 或 "2"(string)，统一归一化：
    int(float(str(raw))) 兼容 int / float / 字符串数字。解析失败返回 0。
    """
    if isinstance(raw, bool):
        return 0
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return 0


def _make_extract_params_node(gateway: Any, emitter: Callable[[dict], None] | None):
    """extract_params 节点工厂：对话追问收集参数。"""

    async def extract_params(state: YuwenState) -> dict:
        _step(emitter, "extract_params", "解析参数", "running")

        visited = list(state.get("nodes_visited") or [])
        if "extract_params" not in visited:
            visited.append("extract_params")

        # 从 state 取消息
        msgs = list(state.get("messages") or [])
        user_msg = state.get("user_message") or state.get("task", "")

        # 构建 LLM 消息：system + 历史 + 当前用户输入
        llm_msgs = [ChatMessage("system", SYSTEM_EXTRACT)]
        # 历史消息（排除 system 和当前 user 的最后一条）
        if msgs:
            for m in msgs:
                if isinstance(m, dict):
                    llm_msgs.append(ChatMessage(m.get("role", "user"), str(m.get("content", ""))))
                elif hasattr(m, "role"):
                    llm_msgs.append(ChatMessage(m.role, m.content))
        # 当前用户消息（如果不在历史中）
        if user_msg:
            last_content = ""
            if msgs:
                last = msgs[-1]
                if isinstance(last, dict):
                    last_content = last.get("content", "")
                elif hasattr(last, "content"):
                    last_content = last.content
            if last_content != user_msg:
                llm_msgs.append(ChatMessage("user", user_msg))

        # 调 LLM 解析参数
        try:
            resp = gateway.chat(llm_msgs, temperature=0.1, json_mode=True)
            parsed = json.loads(resp.content)
        except Exception as exc:
            # LLM 调用失败时的降级
            _step(emitter, "extract_params", "解析参数", "error", str(exc))
            return {
                "yuwen_params": {},
                "yuwen_params_ready": False,
                "final_answer": f"参数解析失败：{exc}，请重试。",
                "nodes_visited": visited,
            }

        title = (parsed.get("title") or "").strip()
        grade_raw = parsed.get("grade", 0)
        grade = _normalize_grade(grade_raw)
        lesson_type = (parsed.get("lesson_type") or "").strip()
        textbook = (parsed.get("textbook") or "").strip()
        question = (parsed.get("question") or "").strip()
        chips = parsed.get("chips") or []

        params_ready = bool(title and 1 <= grade <= 6 and lesson_type)

        if not params_ready:
            # 参数缺失，返回追问。
            # 只发 content 帧（后端 final_answer 对 content 与 token 都累加，
            # 同时发两种会重复累加追问文本；通用对话 call_model 用 content，
            # 追问轮沿用 content 保持一致）。
            if not question:
                question = "请提供课文名和年级，例如：《静夜思》 一年级 古诗词"
            # 追问轮 content 帧携带 chips（LLM 返回的快捷选项），字段名不可变。
            # 前端按 {"type":"content","chips":[...]} 消费。
            content_frame: dict = {"type": "content", "delta": question, "step_id": "extract_params"}
            if isinstance(chips, list) and chips:
                content_frame["chips"] = [str(c) for c in chips]
            _emit(emitter, content_frame)
            _step(emitter, "extract_params", "解析参数", "done", "追问参数")
            return {
                "yuwen_params": {
                    "title": title,
                    "grade": grade if isinstance(grade, int) else 0,
                    "lesson_type": lesson_type or "",
                    "textbook": textbook or "",
                },
                "yuwen_params_ready": False,
                "final_answer": question,
                "nodes_visited": visited,
            }

        # 参数齐备
        params = {
            "title": title,
            "grade": grade,
            "lesson_type": lesson_type,
            "textbook": textbook or f"部编版{grade}年级",
        }
        detail = f"《{title}》· {grade}年级 · {lesson_type}"
        _step(emitter, "extract_params", "解析参数", "done", detail)
        return {
            "yuwen_params": params,
            "yuwen_params_ready": True,
            "nodes_visited": visited,
        }

    return extract_params


# ---------------------------------------------------------------------------
# 条件边：_params_ready
# ---------------------------------------------------------------------------

def _params_ready(state: YuwenState) -> str:
    """条件边：参数齐备 → gen_content；否则 → END。"""
    if state.get("yuwen_params_ready"):
        return "gen_content"
    return "__end__"


# ---------------------------------------------------------------------------
# 节点：gen_content
# ---------------------------------------------------------------------------

SYSTEM_GEN_CONTENT = """你是一个小学语文课件内容生成助手。根据用户提供的课文名、年级、课型，
生成符合课程 JSON Schema 的课件内容。

参考以下规范：

## 学段约束
{stages}

## 课型栏目序列
{lesson_types}

## 课程 JSON Schema
{schema}

## 核心素养
{curriculum}

## 生成要求
1. 严格按照 schema.md 的 JSON 格式输出
2. elements[].type 必须在枚举全集内
3. 每个 objectives[].competency 必须是四素养之一
4. 内容密度参照学段约束（低段字大图多，高段字稍密）
5. 精读课按 period 1/2 分两课时，每课时 15-30 页
6. 输出必须是合法的 JSON 对象（顶层含 version/meta/slides/lessonPlan/handout）
7. 直接输出 JSON，不要用 markdown 代码块包裹
8. 确保 JSON 是纯文本，可以被 json.loads 解析"""


def _make_gen_content_node(gateway: Any, emitter: Callable[[dict], None] | None):
    """gen_content 节点工厂：LLM 按 schema 生成课件 JSON。"""

    async def gen_content(state: YuwenState) -> dict:
        _step(emitter, "gen_content", "生成课件 JSON", "running")

        visited = list(state.get("nodes_visited") or [])
        if "gen_content" not in visited:
            visited.append("gen_content")

        params = state.get("yuwen_params", {})

        # 读取参考文件
        schema_text = _read_ref("schema.md")
        lesson_types_text = _read_ref("lesson-types.md")
        stages_text = _read_ref("stages.md")
        curriculum_text = _read_ref("curriculum.md")

        system_prompt = SYSTEM_GEN_CONTENT.format(
            stages=stages_text,
            lesson_types=lesson_types_text,
            schema=schema_text,
            curriculum=curriculum_text,
        )

        user_prompt = (
            f"请为以下课文生成课件 JSON：\n"
            f"课文名：{params.get('title', '')}\n"
            f"年级：{params.get('grade', '')}\n"
            f"课型：{params.get('lesson_type', '')}\n"
            f"教材版本：{params.get('textbook', '')}\n\n"
            f"直接输出合法的 JSON 对象。"
        )

        # 尝试生成（最多两次）
        content = ""
        for attempt in range(2):
            content = ""
            try:
                async for chunk in gateway.stream_chat(
                    [ChatMessage("system", system_prompt),
                     ChatMessage("user", user_prompt)],
                    temperature=0.4,
                ):
                    if chunk.delta:
                        content += chunk.delta
                        _emit(emitter, {
                            "type": "token",
                            "delta": chunk.delta,
                            "step_id": "gen_content",
                        })
                    if chunk.reasoning:
                        _emit(emitter, {
                            "type": "thinking",
                            "node": "gen_content",
                            "phase": "reasoning",
                            "delta": chunk.reasoning,
                        })
            except Exception as exc:
                if attempt == 0:
                    _emit(emitter, {
                        "type": "token",
                        "delta": f"\n[重试] 生成失败：{exc}，正在重试...\n",
                        "step_id": "gen_content",
                    })
                    continue
                _step(emitter, "gen_content", "生成课件 JSON", "error", str(exc))
                return {
                    "yuwen_content": {},
                    "yuwen_content_path": "",
                    "yuwen_error": f"课件生成失败：{exc}",
                    "final_answer": f"课件生成失败：{exc}",
                    "nodes_visited": visited,
                }

            # 尝试解析 JSON
            doc = None
            try:
                # 尝试直接解析
                doc = json.loads(content)
            except json.JSONDecodeError:
                # 尝试提取 markdown 代码块中的 JSON
                import re
                m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', content, re.DOTALL)
                if m:
                    try:
                        doc = json.loads(m.group(1))
                    except json.JSONDecodeError:
                        pass
            if doc is None:
                # 尝试从第一个 { 到最后一个 }
                start = content.find("{")
                end = content.rfind("}")
                if start >= 0 and end > start:
                    try:
                        doc = json.loads(content[start:end + 1])
                    except json.JSONDecodeError:
                        pass

            if doc is None:
                if attempt == 0:
                    _emit(emitter, {
                        "type": "token",
                        "delta": "\n[JSON 解析失败，正在重试...]\n",
                        "step_id": "gen_content",
                    })
                    continue
                _step(emitter, "gen_content", "生成课件 JSON", "error", "JSON 解析失败")
                return {
                    "yuwen_content": {},
                    "yuwen_content_path": "",
                    "yuwen_error": "课件 JSON 生成失败：无法解析 LLM 输出为合法 JSON。",
                    "final_answer": "课件 JSON 生成失败：无法解析 LLM 输出为合法 JSON。",
                    "nodes_visited": visited,
                }

            # 校验 schema（先归一化：text/question/散装 word-card 等常见
            # 模型偏差自动转换，转换不了的才报错重试）
            from .scripts.common.schema import validate, normalize, SchemaError
            try:
                doc = validate(normalize(doc))
                # 校验通过
                break
            except SchemaError as e:
                if attempt == 0:
                    _emit(emitter, {
                        "type": "token",
                        "delta": f"\n[schema 校验失败：{e}，正在重试...]\n",
                        "step_id": "gen_content",
                    })
                    continue
                _step(emitter, "gen_content", "生成课件 JSON", "error", str(e))
                return {
                    "yuwen_content": {},
                    "yuwen_content_path": "",
                    "yuwen_error": f"课件 JSON schema 校验失败：{e}",
                    "final_answer": f"课件 JSON schema 校验失败：{e}",
                    "nodes_visited": visited,
                }

        # 写入临时 JSON 文件
        session = _session_name(params)
        session_dir = _OUTPUTS_DIR / "yuwen_skill" / session
        session_dir.mkdir(parents=True, exist_ok=True)

        tmp_path = session_dir / "tmp_content.json"
        tmp_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

        n_slides = len(doc.get("slides", []))
        meta = doc.get("meta", {})
        detail = f"{n_slides} slides · {meta.get('periods', '?')} 课时"
        _step(emitter, "gen_content", "生成课件 JSON", "done", detail)

        return {
            "yuwen_content": doc,
            "yuwen_content_path": str(tmp_path),
            "nodes_visited": visited,
        }

    return gen_content


# ---------------------------------------------------------------------------
# 节点：render
# ---------------------------------------------------------------------------

def _make_render_node(emitter: Callable[[dict], None] | None):
    """render 节点工厂：调 render_all.py 产出 pptx/html/docx。"""

    async def render(state: YuwenState) -> dict:
        _step(emitter, "render", "渲染三件套", "running")

        visited = list(state.get("nodes_visited") or [])
        if "render" not in visited:
            visited.append("render")

        content_path = state.get("yuwen_content_path", "")
        params = state.get("yuwen_params", {})
        prior_error = state.get("yuwen_error", "")
        if not content_path or not Path(content_path).exists():
            # content_path 缺失时透传已有错误（gen_content 失败原因优先），
            # 避免把用户可见错误覆盖成笼统的 'content_path missing'。
            if prior_error:
                _step(emitter, "render", "渲染三件套", "error", prior_error)
                return {
                    "yuwen_render_error": prior_error,
                    "nodes_visited": visited,
                }
            _step(emitter, "render", "渲染三件套", "error", "JSON 文件不存在")
            return {
                "yuwen_render_error": "content_path missing",
                "nodes_visited": visited,
            }

        session = _session_name(params)
        session_dir = _OUTPUTS_DIR / "yuwen_skill" / session
        session_dir.mkdir(parents=True, exist_ok=True)

        render_all = _SCRIPTS_DIR / "render_all.py"
        if not render_all.exists():
            _step(emitter, "render", "渲染三件套", "error", "render_all.py 未找到")
            return {
                "yuwen_render_error": "render_all.py not found",
                "nodes_visited": visited,
            }

        try:
            result = subprocess.run(
                [sys.executable, str(render_all), str(content_path), "--out", str(session_dir)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            _step(emitter, "render", "渲染三件套", "error", "超时（120s）")
            return {
                "yuwen_render_error": "timeout",
                "nodes_visited": visited,
            }
        except Exception as exc:
            _step(emitter, "render", "渲染三件套", "error", str(exc))
            return {
                "yuwen_render_error": str(exc),
                "nodes_visited": visited,
            }

        if result.returncode not in (0,):
            err_msg = (result.stderr or "").strip() or f"退出码 {result.returncode}"
            # 非零退出码时，render_all.py 的三个渲染器各自 try/except，
            # 部分成功的文件已在磁盘。glob 输出目录，产物非空即部分成功。
            partial_files = []
            for ext, mime in [
                (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
                (".html", "text/html"),
                (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            ]:
                for fp in sorted(session_dir.glob(f"*{ext}")):
                    size = fp.stat().st_size
                    partial_files.append({
                        "name": fp.name,
                        "path": f"/files/yuwen_skill/{session}/{fp.name}",
                        "size": size,
                        "mime": mime,
                    })

            if partial_files:
                # 部分成功：files 帧照发，report 注明哪个渲染器失败
                _step(emitter, "render", "渲染三件套", "done", f"部分成功（{err_msg}）")
                return {
                    "yuwen_files": partial_files,
                    "yuwen_render_error": err_msg,
                    "nodes_visited": visited,
                }

            _step(emitter, "render", "渲染三件套", "error", err_msg)
            return {
                "yuwen_render_error": err_msg,
                "nodes_visited": visited,
            }

        # 收集输出文件
        files = []
        for ext, mime in [
            (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            (".html", "text/html"),
            (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ]:
            for fp in sorted(session_dir.glob(f"*{ext}")):
                size = fp.stat().st_size
                files.append({
                    "name": fp.name,
                    "path": f"/files/yuwen_skill/{session}/{fp.name}",
                    "size": size,
                    "mime": mime,
                })

        detail = "pptx/html/docx 退出码 0" if result.returncode == 0 else "渲染异常"
        _step(emitter, "render", "渲染三件套", "done", detail)

        return {
            "yuwen_files": files,
            "nodes_visited": visited,
        }

    return render


# ---------------------------------------------------------------------------
# 节点：report
# ---------------------------------------------------------------------------

def _make_report_node(emitter: Callable[[dict], None] | None):
    """report 节点工厂：汇总交付结果，推 files/done 帧。"""

    async def report(state: YuwenState) -> dict:
        _step(emitter, "report", "交付报告", "running")

        visited = list(state.get("nodes_visited") or [])
        if "report" not in visited:
            visited.append("report")

        files = state.get("yuwen_files", [])
        # 优先展示真实失败原因：gen_content 失败(yuwen_error)优先于 render 失败
        error = state.get("yuwen_error", "") or state.get("yuwen_render_error", "")
        params = state.get("yuwen_params", {})
        session = _session_name(params)

        # 部分成功：有文件但渲染器有失败 → files 帧照发，report 注明哪个渲染器失败
        if error and files:
            _emit(emitter, {"type": "files", "files": files})
            file_names = " / ".join(f["name"] for f in files)
            answer = f"课件部分生成成功（{len(files)} 个文件：{file_names}），但 {error}"
            detail = f"部分成功：{error}"
            _step(emitter, "report", "交付报告", "done", detail)
            _emit(emitter, {
                "type": "done",
                "answer": answer,
                "meta": {
                    "nodes_visited": visited,
                    "audit_total": len(visited),
                },
            })
            return {
                "final_answer": answer,
                "nodes_visited": visited,
                "messages": state.get("messages", []) + [
                    {"role": "assistant", "content": answer},
                ],
            }

        if error:
            answer = f"课件生成失败：{error}"
            _emit(emitter, {"type": "content", "delta": answer, "step_id": "report"})
            # error 终态：保证每个 running 都有终态（前端时间线避免永久"运行中"）
            _step(emitter, "report", "交付报告", "error", error)
            return {
                "final_answer": answer,
                "nodes_visited": visited,
            }

        # 推 files 帧
        if files:
            _emit(emitter, {"type": "files", "files": files})

        # 构建 summary
        n_files = len(files)
        if n_files > 0:
            file_names = " / ".join(f["name"] for f in files)
            answer = f"课件已生成，共 {n_files} 个文件：{file_names}"
            detail = f"已写入 outputs/yuwen_skill/{session}/"
        else:
            answer = "课件内容已生成，但渲染未产出文件。"
            detail = "无产出文件"

        _step(emitter, "report", "交付报告", "done", detail)

        # 推 done 帧
        _emit(emitter, {
            "type": "done",
            "answer": answer,
            "meta": {
                "nodes_visited": visited,
                "audit_total": len(visited),
            },
        })

        return {
            "final_answer": answer,
            "nodes_visited": visited,
            "messages": state.get("messages", []) + [
                {"role": "assistant", "content": answer},
            ],
        }

    return report


# ---------------------------------------------------------------------------
# 图组装
# ---------------------------------------------------------------------------

def build_graph(
    gateway: Any,
    registry: Any,
    audit: Any | None = None,
    emitter: Callable[[dict], None] | None = None,
) -> Any:
    """组装并编译语文智能体 langgraph 图。

    参数：
        gateway:  模型网关（gateway.chat / gateway.stream_chat）
        registry: Skill 注册中心（本图暂不使用）
        audit:    审计日志（可选）
        emitter:  事件回调，节点把帧推给 web 层

    返回：
        langgraph 编译后的图，可 .astream(input) 异步流式执行。
    """
    graph = StateGraph(YuwenState)

    # 注册节点
    graph.add_node("extract_params", _make_extract_params_node(gateway, emitter))
    graph.add_node("gen_content", _make_gen_content_node(gateway, emitter))
    graph.add_node("render", _make_render_node(emitter))
    graph.add_node("report", _make_report_node(emitter))

    # 入口
    graph.set_entry_point("extract_params")

    # extract_params 条件出边：参数齐备 → gen_content；否则 → END
    graph.add_conditional_edges(
        "extract_params",
        _params_ready,
        {
            "gen_content": "gen_content",
            "__end__": END,
        },
    )

    # 主链
    graph.add_edge("gen_content", "render")
    graph.add_edge("render", "report")
    graph.add_edge("report", END)

    return graph.compile()