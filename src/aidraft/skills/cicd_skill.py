"""CI/CD Skill：流水线触发与状态查询（Jenkins REST API）。

把 CI/CD 流水线封装为标准化 AI Skill。

设计要点：
1. Skill 抽象如何对应 MCP Server 的 tools/resources/prompts：
   - trigger_pipeline/get_pipeline_status/fetch_test_report 即 MCP Server 暴露的三个 tools。
   - get_pipeline_status/fetch_test_report 的输出可同时作为 MCP resources
     （按 run_id 索引的"测试报告"资源）暴露。
2. 低代码理念：把 Jenkins 换成 GitLab CI，只需新写一个 CICDSkill 子类覆盖这几个方法，
   registry 与 Agent 完全复用——新系统接入零侵入。
3. 高危动作对接 governance 审批门：trigger_pipeline 触发流水线可能部署到生产，
   属高危，应在执行前调 governance.ApprovalGate.request() 走人工审批。

实现要求：用 requests（惰性导入）调 Jenkins REST API（base_url + token + crumb）。
无凭证时返回明确提示，不崩溃。所有方法返回 str。
"""
from __future__ import annotations

import os
from typing import Any

from .registry import SkillSpec


class CICDSkill:
    """CI/CD 流水线 Skill：把 Jenkins 封装为标准化 AI 能力。

    把 CI/CD 流水线封装为标准化 AI Skill。
    """

    name = "cicd"

    def __init__(self, base_url: str = "", token: str = "") -> None:
        """初始化 CI/CD Skill。

        Args:
            base_url: Jenkins 根 URL，如 "http://jenkins.example.com"。
                缺省从环境变量 JENKINS_URL 读，缺失则方法返回降级提示。
            token: Jenkins API Token（用户设置里生成）。缺省从
                JENKINS_TOKEN 读。需配合 JENKINS_USER 使用（HTTP Basic Auth）。
        """
        # 凭证优先环境变量：硬性要求"凭证/Token 一律从环境变量读"。
        self._base_url: str = (base_url or os.getenv("JENKINS_URL", "")).rstrip("/")
        self._token: str = token or os.getenv("JENKINS_TOKEN", "")
        self._user: str = os.getenv("JENKINS_USER", "")
        self._crumb: str = ""  # CSRF crumb，按需懒加载缓存

    # ------------------------------------------------------------------
    # 能力清单：对应 MCP Server tools 列表。
    # ------------------------------------------------------------------
    def specs(self) -> list[SkillSpec]:
        """返回本 Skill 暴露的能力清单（对应 MCP Server tools）。"""
        return [
            SkillSpec(
                name="trigger_pipeline",
                description="触发指定流水线运行（高危，需 Human-on-the-Loop 审批）",
                func=self.trigger_pipeline,
                schema={"job": {"type": "string"}, "params": {"type": "object"}},
            ),
            SkillSpec(
                name="get_pipeline_status",
                description="查询流水线运行状态与日志（只读）",
                func=self.get_pipeline_status,
                schema={"run_id": {"type": "string"}},
            ),
            SkillSpec(
                name="fetch_test_report",
                description="拉取测试报告，解析失败用例（只读）",
                func=self.fetch_test_report,
                schema={"run_id": {"type": "string"}},
            ),
        ]

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _check_creds(self) -> str | None:
        """校验凭证是否齐全。返回 None 表示通过，否则返回降级提示字符串。"""
        if not self._base_url:
            return "[cicd] 未配置 JENKINS_URL，无法调用 Jenkins API。"
        if not self._token or not self._user:
            return "[cicd] 未配置 JENKINS_USER/JENKINS_TOKEN，无法鉴权。"
        return None

    def _import_requests(self) -> Any:
        """惰性导入 requests，保证顶层 import 本模块无需装 requests。"""
        try:
            import requests  # type: ignore
        except ImportError as exc:
            raise RuntimeError("requests 未安装（pip install requests 后可用）") from exc
        return requests

    def _auth(self) -> tuple[str, str]:
        """返回 HTTP Basic Auth 元组。"""
        return self._user, self._token

    def _get_crumb(self) -> str:
        """获取 Jenkins CSRF crumb（POST 类操作必需）。

        Jenkins 默认开启 CSRF 保护，POST 请求需带 `Jenkins-Crumb` 头。
        懒加载并缓存，避免每次触发都多打一次请求。
        """
        if self._crumb:
            return self._crumb
        requests = self._import_requests()
        url = f"{self._base_url}/crumbIssuer/api/json"
        # 用 Basic Auth 取 crumb。
        resp = requests.get(url, auth=self._auth(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # crumb 字段名标准为 crumb；同时缓存到实例。
        self._crumb = data.get("crumb", "")
        return self._crumb

    def _crumb_header(self) -> dict[str, str]:
        """返回带 crumb 的请求头。"""
        try:
            return {"Jenkins-Crumb": self._get_crumb()}
        except Exception as exc:  # noqa: BLE001 - crumb 失败不阻断，让 POST 自行报错
            # 取 crumb 失败时仍尝试发 POST，便于某些关闭 CSRF 的实例。
            return {}

    # ------------------------------------------------------------------
    # 能力实现
    # ------------------------------------------------------------------
    def trigger_pipeline(self, job: str, params: dict | None = None) -> str:
        """触发指定流水线运行（高危写操作）。

        ===== 高危动作 =====
        此方法应在执行前调 governance.ApprovalGate.request() 触发人工审批
        （Human-on-the-Loop）：触发流水线可能部署到生产环境，必须人工确认。
        本实现以注释标注接入点；生产应在调度层统一拦截高危 specs 强制走审批门。
        ===================

        用 Jenkins `buildWithParameters`（带参数）或 `build`（无参数）API。
        """
        creds_err = self._check_creds()
        if creds_err:
            return creds_err
        if not job:
            return "[cicd] trigger_pipeline: empty job name"

        # ---- governance 接入点（伪代码）-----------------------------------
        # from ..governance.approval import ApprovalGate
        # ApprovalGate.request(
        #     action="cicd.trigger_pipeline",
        #     summary=f"trigger Jenkins job {job} with {params}",
        #     risk="high",  # 可能部署生产
        # ).require_approved()
        # ------------------------------------------------------------------

        try:
            requests = self._import_requests()
            # URL 编码 job 名（支持文件夹路径如 "folder/job"）。
            job_url = f"{self._base_url}/job/{job.replace('/', '/job/')}"
            params = params or {}
            if params:
                # 带参数构建：buildWithParameters 接收 query string。
                url = f"{job_url}/buildWithParameters"
                resp = requests.post(
                    url, params=params, auth=self._auth(),
                    headers=self._crumb_header(), timeout=30,
                )
            else:
                # 无参数构建：直接 build。
                url = f"{job_url}/build"
                resp = requests.post(
                    url, auth=self._auth(),
                    headers=self._crumb_header(), timeout=30,
                )
            # Jenkins 触发成功返回 201，并在 Location 头里给出 queue item URL。
            if resp.status_code in (200, 201):
                queue_url = resp.headers.get("Location", "(unknown)")
                return f"[cicd] triggered {job}: queue={queue_url}"
            return (
                f"[cicd] trigger {job} failed: "
                f"HTTP {resp.status_code} {resp.text[:200]}"
            )
        except Exception as exc:  # noqa: BLE001 - 返回 str 不崩溃
            return f"[cicd] trigger_pipeline error: {exc}"

    def get_pipeline_status(self, run_id: str) -> str:
        """查询流水线运行状态与日志（只读）。

        run_id 约定格式 "<job>/<build_number>"，例如 "devpilot-ci/42"。
        流程：
        - 直接查 build json 拿状态；若 run_id 形如 "queue/<queueId>" 则先查
          queue/item 拿到实际 build 号再查。
        """
        creds_err = self._check_creds()
        if creds_err:
            return creds_err
        if not run_id:
            return "[cicd] get_pipeline_status: empty run_id"

        try:
            requests = self._import_requests()
            # 解析 run_id：支持 "queue/<id>" 与 "<job>/<build>" 两种形式。
            if run_id.startswith("queue/"):
                queue_id = run_id.split("/", 1)[1]
                # 查 queue/item 拿执行号。
                q_url = f"{self._base_url}/queue/item/{queue_id}/api/json"
                q_resp = requests.get(q_url, auth=self._auth(), timeout=15)
                if q_resp.status_code != 200:
                    return f"[cicd] queue item {queue_id}: HTTP {q_resp.status_code}"
                q_data = q_resp.json()
                exec_ref = q_data.get("executable", {})
                number = exec_ref.get("number")
                url = exec_ref.get("url", "")
                if not number or not url:
                    # 还在排队：尚未分配执行号。
                    why = q_data.get("why", "still in queue")
                    return f"[cicd] run {run_id}: {why}"
                # 用 build URL 继续查状态。
                build_url = f"{url}api/json"
            else:
                # "job/<job>/<build>/api/json" 形式。
                job, _, number = run_id.partition("/")
                if not number:
                    return "[cicd] run_id format should be '<job>/<build_number>' or 'queue/<id>'"
                job_url = f"{self._base_url}/job/{job.replace('/', '/job/')}"
                build_url = f"{job_url}/{number}/api/json"

            b_resp = requests.get(build_url, auth=self._auth(), timeout=15)
            if b_resp.status_code != 200:
                return f"[cicd] build {run_id}: HTTP {b_resp.status_code}"
            b = b_resp.json()
            # 提取关键字段：结果、是否构建中、耗时、URL。
            result = b.get("result", "IN_PROGRESS") or "IN_PROGRESS"
            building = b.get("building", False)
            duration = b.get("duration", 0)
            build_web_url = b.get("url", "")
            # 拉取最近一段 console log 便于 Agent 诊断（截断防止超长）。
            log_url = build_url.replace("/api/json", "/consoleText")
            log_resp = requests.get(log_url, auth=self._auth(), timeout=15)
            log_tail = (log_resp.text or "")[-1500:] if log_resp.status_code == 200 else ""
            status_line = (
                f"[cicd] run {run_id}: result={result}, building={building}, "
                f"duration={duration}ms, url={build_web_url}"
            )
            if log_tail:
                status_line += "\n---- console tail ----\n" + log_tail
            return status_line
        except Exception as exc:  # noqa: BLE001
            return f"[cicd] get_pipeline_status error: {exc}"

    def fetch_test_report(self, run_id: str) -> str:
        """拉取测试报告，解析失败用例（只读）。

        run_id 形如 "<job>/<build_number>"。调 Jenkins testReport API，
        把失败的 cases 整理成可读文本供 Agent 进上下文。
        """
        creds_err = self._check_creds()
        if creds_err:
            return creds_err
        if not run_id:
            return "[cicd] fetch_test_report: empty run_id"

        try:
            requests = self._import_requests()
            job, _, number = run_id.partition("/")
            if not number:
                return "[cicd] run_id format should be '<job>/<build_number>'"
            job_url = f"{self._base_url}/job/{job.replace('/', '/job/')}"
            # testReport API 返回该次构建的测试结果汇总。
            url = f"{job_url}/{number}/testReport/api/json"
            resp = requests.get(url, auth=self._auth(), timeout=30)
            if resp.status_code != 200:
                return f"[cicd] testReport {run_id}: HTTP {resp.status_code}"
            data = resp.json()
            # 汇总计数。
            total = data.get("totalCount", 0)
            fail = data.get("failCount", 0)
            skip = data.get("skipCount", 0)
            lines = [f"[cicd] testReport {run_id}: total={total}, fail={fail}, skip={skip}"]
            # 遍历 suites 找出失败的 case，输出 className/name/errorDetails。
            for suite in data.get("suites", []):
                for case in suite.get("cases", []):
                    status = case.get("status", "")
                    if status in ("FAILED", "REGRESSION"):
                        cls = case.get("className", "?")
                        name = case.get("name", "?")
                        err = case.get("errorDetails", "")
                        age = case.get("age", 0)
                        lines.append(
                            f"  FAIL {cls}.{name} (age={age}): {err}"
                        )
            if fail == 0:
                lines.append("  (no failed cases)")
            return "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            return f"[cicd] fetch_test_report error: {exc}"
