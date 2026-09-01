"""语文智能体共享状态与基础设施：YuwenState / 路径常量 / 帧辅助。

被 graph.py 与 nodes/ 各节点模块 import（单一来源，无业务逻辑）：
- YuwenState：langgraph 图状态（增量合并，节点只返回自己改的字段）
- 路径常量：_SCRIPTS_DIR / _REFERENCES_DIR / _OUTPUTS_DIR（渲染脚本、
  参考契约、交付物落盘根目录）
- _session_name：从参数生成会话目录名
- _state_path / _save_state / _load_state：跨轮状态落盘（state.json）
- _emit / _step：SSE 帧推送辅助（推给 web 层）
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Callable

from typing_extensions import TypedDict

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
    yuwen_outline: dict          # {pages: [{id, kind, title, points, period}], meta: {...}}
    yuwen_outline_confirmed: bool  # confirm 节点判定用户确认后 True
    yuwen_review: dict           # {scores: {dim: int}, issues: [{page_id, problems}], pass: bool}
    yuwen_revise_rounds: int     # revise 已用轮数（≤2）
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
_PROJECT_ROOT = _THIS_DIR.parents[3]  # yuwen → agenthub → devpilot → src → 项目根
# 课件交付物落盘根目录。优先 DEVPILOT_OUTPUTS_DIR（Docker 里 pip install
# 非 editable 安装时 __file__ 位于 site-packages，parents[3] 推断出
# /usr/local/lib/python3.13 这类只读假项目根，mkdir 直接 PermissionError；
# 与 web/api.py 的 DEVPILOT_DIST_DIR 同一套约定，compose 挂载 ./outputs）。
_OUTPUTS_DIR = Path(os.environ.get("DEVPILOT_OUTPUTS_DIR")
                    or _PROJECT_ROOT / "outputs")


def _session_name(params: dict) -> str:
    """从参数生成会话目录名（安全文件名）。"""
    title = params.get("title", "untitled")
    lesson_type = params.get("lesson_type", "unknown")
    safe_title = "".join(c for c in title if c not in '\\/:*?"<>|')
    safe_lt = "".join(c for c in lesson_type if c not in '\\/:*?"<>|')
    return f"{safe_title}-{safe_lt}"


# ---------------------------------------------------------------------------
# 跨轮状态落盘（state.json）
# ---------------------------------------------------------------------------
# 多阶段管线里 gen_outline → END 之后，图实例销毁；用户下一轮消息重建新图，
# 大纲必须能跨轮找回。langgraph 无 checkpointer（每轮 state 从零开始），
# 所以用磁盘 JSON 做跨轮状态存储——session 目录名由 params 派生，与
# render/report 同键，天然一致。并发不用考虑（单用户单会话串行）。

def _state_path(params: dict) -> Path:
    """state.json 落盘路径（session 目录下）。"""
    return _OUTPUTS_DIR / "yuwen" / _session_name(params) / "state.json"


def _session_dir(params: dict) -> Path:
    """session 交付目录（outputs/yuwen/<会话名>/）。"""
    return _OUTPUTS_DIR / "yuwen" / _session_name(params)


def _content_path(params: dict) -> Path:
    """tmp_content.json（完整课程 JSON）落盘路径。

    render 节点经 yuwen_content_path 读的就是这个文件；gen_slides 初写，
    gen_plan / revise / gen_images 更新 doc 后都重写到这里，保证 render
    子进程拿到的永远是最新版。
    """
    return _OUTPUTS_DIR / "yuwen" / _session_name(params) / "tmp_content.json"


def _save_state(params: dict, **fields) -> None:
    """读-改-写 state.json：不存在则初始化 {}，只更新传入字段。

    异常吞掉不阻断主流程——state.json 是跨轮恢复的辅助通道，
    写失败最多让用户重跑一轮，不该炸掉正在进行的生成。
    """
    try:
        path = _state_path(params)
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (json.JSONDecodeError, OSError):
                data = {}
        data.update(fields)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except Exception:  # noqa: BLE001 - 落盘失败不阻断管线
        pass


def _load_state(params: dict) -> dict:
    """读 state.json；不存在 / 损坏 / 非对象一律返回 {}（防御式）。"""
    try:
        path = _state_path(params)
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# LLM JSON 输出解析 / outline 帧
# ---------------------------------------------------------------------------

def _parse_llm_json(content: str):
    """从 LLM 输出中提取 JSON（对象或数组），三级降级：

    1. 直接 json.loads
    2. markdown 代码块 ```json ... ```
    3. 首个 {/[ 到末个 }/] 截取
    全部失败抛 ValueError（调用方按解析失败处理）。
    """
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', str(content), re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    s = str(content)
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start, end = s.find(open_ch), s.rfind(close_ch)
        if start >= 0 and end > start:
            try:
                return json.loads(s[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("LLM 输出无法解析为 JSON")


def _outline_summary(outline: dict) -> str:
    """outline 的人类可读摘要（content 帧用，旧前端不认识 outline 帧时至少可读）。"""
    pages = outline.get("pages") or []
    meta = outline.get("meta") or {}
    periods = meta.get("periods", 1)
    title = meta.get("title", "")
    return (f"《{title}》大纲已生成，共 {len(pages)} 页（{periods} 课时）。"
            f"回复\"确认\"开始生成，或直接说修改意见（如\"第3页改成……\"\"换青蓝主题\"）。")


# 大纲应答指令词表（confirm 路由兜底用）：用户点 chip 或打确认/切主题词时，
# extract_params 的 LLM 可能抽不出参数（params_ready=False），若直接 END，
# 跨轮状态机就卡在确认环节。路由层用这张词表识别"这是对大纲的回应"，
# 仍路由 confirm，由 confirm 从盘上（_find_pending_session）找回会话。
_CONFIRM_WORDS = ("确认", "可以", "没问题", "开始生成", "直接生成", "就这样",
                  "同意", "ok", "OK", "好", "行", "继续")
_THEME_WORDS = ("blue", "fresh", "green", "warm", "主题", "蓝色", "青蓝",
                "绿色", "墨绿", "橙色", "默认")


def _looks_like_outline_command(msg: str) -> bool:
    """消息是否像对大纲的应答（确认/切主题指令）——确定性关键词判定。"""
    s = (msg or "").strip()
    if not s or len(s) > 40:
        return False
    if any(w in s for w in _THEME_WORDS):
        return True
    # 确认词要求整句短且不含新课文信息信号（书名号）——"确认大纲，开始生成"
    # 命中；"确认《静夜思》课件参数"这种含书名号的走正常 extract_params。
    return ("《" not in s) and any(w in s for w in _CONFIRM_WORDS)


def _find_pending_session() -> tuple[dict, dict] | None:
    """扫描 _OUTPUTS_DIR/yuwen/*/state.json，找最近更新的未确认大纲会话。

    返回 (params, disk_state)；无未确认大纲返回 None。单用户场景下
    "最近修改 + 未确认"足以唯一定位待确认会话。异常吞掉返回 None。
    """
    try:
        root = _OUTPUTS_DIR / "yuwen"
        if not root.exists():
            return None
        best: tuple[float, dict, dict] | None = None  # (mtime, params, state)
        for sub in root.iterdir():
            sp = sub / "state.json"
            if not sp.exists():
                continue
            try:
                data = json.loads(sp.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict) or not data.get("yuwen_outline"):
                continue
            if data.get("yuwen_outline_confirmed"):
                continue
            mtime = sp.stat().st_mtime
            if best is None or mtime > best[0]:
                params = data.get("yuwen_params") or {}
                best = (mtime, params, data)
        if best is None:
            return None
        return best[1], best[2]
    except Exception:  # noqa: BLE001 - 扫描失败按无待确认会话处理
        return None


def _emit_outline(emitter: Callable[[dict], None] | None, outline: dict) -> None:
    """发 outline 帧 + 摘要 content 帧（帧契约见 graph.py docstring）。

    只发 content 不发 token（api.py 对两者都累加 final_answer，重复发会翻倍）。
    """
    _emit(emitter, {
        "type": "outline",
        "outline": outline,
        "chips": ["确认大纲，开始生成", "第1页改成…", "换青蓝主题", "换墨绿主题"],
    })
    _emit(emitter, {
        "type": "content",
        "delta": _outline_summary(outline),
        "step_id": "gen_outline",
    })


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
