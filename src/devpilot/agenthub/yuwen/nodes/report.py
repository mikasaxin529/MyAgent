"""report 节点：汇总文件清单，推 files / done 终帧。"""
from __future__ import annotations

from typing import Callable

from ..state import YuwenState, _emit, _session_name, _step


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
            detail = f"已写入 outputs/yuwen/{session}/"
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
