"""render 节点：调 scripts/render_all.py 产出 pptx / html / docx 三件套。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable

from ..state import (
    YuwenState,
    _OUTPUTS_DIR,
    _SCRIPTS_DIR,
    _session_name,
    _step,
)


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
        session_dir = _OUTPUTS_DIR / "yuwen" / session
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
                        "path": f"/files/yuwen/{session}/{fp.name}",
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
                    "path": f"/files/yuwen/{session}/{fp.name}",
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
