"""语文智能体共享状态与基础设施：YuwenState / 路径常量 / 帧辅助。

被 graph.py 与 nodes/ 各节点模块 import（单一来源，无业务逻辑）：
- YuwenState：langgraph 图状态（增量合并，节点只返回自己改的字段）
- 路径常量：_SCRIPTS_DIR / _REFERENCES_DIR / _OUTPUTS_DIR（渲染脚本、
  参考契约、交付物落盘根目录）
- _session_name：从参数生成会话目录名
- _emit / _step：SSE 帧推送辅助（推给 web 层）
"""
from __future__ import annotations

import os
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
