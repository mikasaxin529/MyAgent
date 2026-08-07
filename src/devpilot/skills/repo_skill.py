"""Repo Skill：代码仓库操作（本地 git 优先 + GitHub PR 可选）。

通过 MCP/A2A 协议把内部系统（代码仓库、CI/CD、项目管理）封装为
标准化 AI Skills，构建低代码 Skill 框架与企业级可复用技能生态。

设计要点：
1. Skill 抽象如何对应 MCP Server 的 tools/resources/prompts：
   - 一个 Skill 类 = 一个"内部系统适配器"，对应 MCP Server 暴露的一组 tools。
   - SkillSpec.name/description/func/schema 直接映射 MCP tool 的 name/description/callback/inputSchema。
   - specs() 返回的列表即 MCP Server 的 tools 列表；registry.all_specs() 聚合后可一次性
     注册成 MCP Server，或通过 A2A 协议广播给其他 Agent。
   - read_file/search_code 这种"只读"能力可同时作为 MCP resources（按 URI 索引）暴露。
2. 低代码理念：新系统接入只需实现一个 Skill 类 + specs()，无需改 Agent/调度/审计逻辑。
   例如把 GitHub 换成 GitLab，只需新写一个 RepoSkill 子类覆盖 commit_and_pr 即可，
   registry 与 Agent 完全复用——这就是"低代码 Skill 框架"的核心收益。
3. 高危动作对接 governance 审批门：commit_and_pr 在执行前应调用
   governance.ApprovalGate.request() 触发人工审批（Human-on-the-Loop），
   审批通过后才真正执行写操作。本实现以注释标注接入点，便于说明设计。

W4 实现：优先用本地 git（subprocess 调 git 命令），无需 GitHub Token 也可 demo；
PR 部分若配置了 GITHUB_TOKEN 则惰性用 PyGithub 真实发 PR，否则返回明确提示。
"""
from __future__ import annotations

import os
import subprocess
from typing import Any

from .registry import SkillSpec


class RepoSkill:
    """代码仓库 Skill：把"代码仓库"这套内部系统封装为标准化 AI 能力。

    为什么优先本地 git 而非 GitHub API：
    - 本地 git（subprocess）零依赖、零 Token，开箱即可 demo，契合"低代码"快速接入理念。
    - PR 等远端写操作才按需走 PyGithub，体现"凭证缺失时优雅降级"的工程原则。

    把代码仓库封装为标准化 AI Skill。
    """

    name = "repo"

    def __init__(self, token: str = "", repo: str = "", repo_path: str = ".") -> None:
        """初始化仓库 Skill。

        Args:
            token: GitHub Personal Access Token，用于真实发 PR。从环境变量
                GITHUB_TOKEN 读取更安全；为空则降级为"仅本地提交"。
            repo: 远端仓库名，格式 "owner/repo"（如 "octocat/Hello-World"），
                发 PR 时必填。
            repo_path: 本地仓库工作区路径，本地 git 命令的 cwd。默认 "."
                表示当前目录，便于 demo。
        """
        # 凭证优先从环境变量读，构造函数显式传入可覆盖（便于测试）。
        # 对应硬性要求：凭证/Token 一律从环境变量读，缺失时优雅降级。
        self._token: str = token or os.getenv("GITHUB_TOKEN", "")
        self._repo: str = repo or os.getenv("GITHUB_REPO", "")
        # repo_path 用绝对路径，避免后续 subprocess 的 cwd 相对路径歧义。
        self._repo_path: str = repo_path

    # ------------------------------------------------------------------
    # 能力清单：specs() 是 Skill 抽象的核心，对应 MCP Server 的 tools 列表。
    # 每个 SkillSpec = 一个 MCP tool：name/description/func/schema。
    # 新系统接入只需重写 specs() 与对应方法，Agent/调度/审计完全复用。
    # ------------------------------------------------------------------
    def specs(self) -> list[SkillSpec]:
        """返回本 Skill 暴露的能力清单（对应 MCP Server tools）。

        设计原则：保持公开接口签名不变（specs 返回结构稳定），新增能力以追加项方式扩展，
        避免破坏既有 Agent 的 tool 调用约定。
        """
        return [
            SkillSpec(
                name="read_file",
                description="读取仓库内指定路径文件内容（只读，对应 MCP resource）",
                func=self.read_file,
                schema={"path": {"type": "string"}},
            ),
            SkillSpec(
                name="search_code",
                description="在仓库中搜索代码片段（git grep，只读）",
                func=self.search_code,
                schema={"query": {"type": "string"}},
            ),
            SkillSpec(
                name="create_branch",
                description="创建分支（修改代码前置步骤，本地 git 写操作）",
                func=self.create_branch,
                schema={"branch": {"type": "string"}},
            ),
            SkillSpec(
                name="commit_and_pr",
                description="提交改动并发起 PR（高危，需 Human-on-the-Loop 审批）",
                func=self.commit_and_pr,
                schema={"branch": {"type": "string"}, "message": {"type": "string"}},
            ),
        ]

    # ------------------------------------------------------------------
    # 内部工具：统一执行 git 命令，捕获异常并返回可直接进上下文的字符串。
    # ------------------------------------------------------------------
    def _run_git(self, args: list[str]) -> str:
        """在本地仓库路径下执行 git 子命令，返回 stdout 文本。

        为什么集中封装：
        - 统一异常转字符串，避免 Agent 因 git 报错而崩溃（所有方法返回 str 的硬性要求）。
        - 统一 cwd=self._repo_path，避免每个方法重复指定工作区。
        - 便于后续在审计/日志层统一埋点（对应 registry 的"审计 Skill 调用"职责）。
        """
        try:
            # subprocess.run 捕获 stdout/stderr 文本；check=False 让我们手动处理失败。
            # text=True 自动按系统编码解码，便于直接拼进 Agent 上下文。
            result = subprocess.run(
                ["git", *args],
                cwd=self._repo_path,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            # 系统未安装 git：降级为明确提示，不抛异常。
            return f"[repo] git not found: {exc}"
        except Exception as exc:  # noqa: BLE001 - 兜底，确保返回 str
            return f"[repo] git command error: {exc}"

        if result.returncode != 0:
            # git 命令失败：把 stderr 返回给 Agent，便于其自诊断/重试。
            return f"[repo] git {' '.join(args)} failed (exit={result.returncode}): {result.stderr.strip()}"
        return result.stdout

    # ------------------------------------------------------------------
    # 能力实现
    # ------------------------------------------------------------------
    def read_file(self, path: str) -> str:
        """读取仓库内指定路径的文件内容。

        实现策略：优先直接 open 工作区文件（最简单、最快）；
        若文件不存在则退回 `git show HEAD:path` 读暂存区/版本库中的副本，
        这样即使工作区被清理也能读到最近一次提交的版本。

        对应 MCP：既是一个 tool（按 path 参数读），也可作为 resource
        （按 file://path URI 索引）暴露给 Agent。
        """
        # 先尝试直接读工作区文件——零依赖、零开销。
        full_path = os.path.join(self._repo_path, path)
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except FileNotFoundError:
            # 工作区没有该文件，可能是路径只在某个提交里存在，用 git show 读版本库。
            pass
        except Exception as exc:  # noqa: BLE001 - 兜底降级
            return f"[repo] read_file({path}) error: {exc}"

        # 退路：git show HEAD:<path> 读取最近一次提交中的文件内容。
        return self._run_git(["show", f"HEAD:{path}"]) or f"[repo] file not found: {path}"

    def search_code(self, query: str) -> str:
        """在仓库中搜索代码片段，返回命中行（带行号与文件路径）。

        用 `git grep -n` 而非 GitHub Search API：本地即可执行、无需 Token、
        命中结果即当前工作区状态（包含未提交改动），更贴近"改代码→跑测试"的现场。
        """
        if not query:
            # 空查询防御：避免 git grep 把后续参数当 pattern 误解析。
            return "[repo] search_code: empty query"
        # -n 显示行号；-I 跳过二进制文件；--no-color 避免 ANSI 控制符污染上下文。
        return self._run_git(["grep", "-n", "-I", "--no-color", query])

    def create_branch(self, branch: str) -> str:
        """创建并切换到新分支（修改代码的前置步骤）。

        用 `git checkout -b <branch>`：本地写操作，无需远端 Token。
        失败（如分支已存在）时返回 git 的 stderr，由 Agent 决定是否换名重试。
        """
        if not branch:
            return "[repo] create_branch: empty branch name"
        # checkout -b 创建并切换；若需基于特定起点可扩展为接受 base 参数。
        out = self._run_git(["checkout", "-b", branch])
        # 附带当前分支确认信息，便于 Agent 校验状态。
        return out + self._run_git(["branch", "--show-current"])

    def commit_and_pr(self, branch: str, message: str) -> str:
        """提交改动并发起 PR（高危写操作）。

        ===== 高危动作 =====
        此方法应在执行前调 governance.ApprovalGate.request() 触发人工审批
        （Human-on-the-Loop）。审批通过后才执行 git add/commit 与发 PR。
        本实现以注释标注接入点，实际生产应在 Skill 调度层统一拦截高危 specs
        并强制走审批门，避免每个 Skill 重复实现。
        ===================

        实现分两段：
        1. 本地提交：git add -A + git commit -m，零 Token 也可完成。
        2. 发 PR：若配置了 GITHUB_TOKEN 则惰性 import PyGithub 真实发 PR；
           否则返回"已本地提交，PR 需配置 token"的明确提示，不抛异常。

        高危动作如何对接 governance 审批门——此方法是典型示例。
        """
        if not branch or not message:
            return "[repo] commit_and_pr: branch and message are required"

        # ---- governance 接入点（伪代码，便于说明设计）----------------------
        # from ..governance.approval import ApprovalGate
        # ApprovalGate.request(
        #     action="repo.commit_and_pr",
        #     summary=f"commit on {branch}: {message}",
        #     risk="high",  # 写远端仓库、可能触发 CI/CD
        # ).require_approved()  # 未审批则抛 ApprovalPending，由调度层转为人工确认卡
        # ------------------------------------------------------------------

        # 1) 本地提交：先切到目标分支，再 add 全部改动并 commit。
        self._run_git(["checkout", branch])  # 已存在则切换；不存在会失败，Agent 可先调 create_branch
        self._run_git(["add", "-A"])
        commit_out = self._run_git(["commit", "-m", message])
        # 附带最近一次提交摘要，便于 Agent 校验提交是否成功。
        log_out = self._run_git(["log", "-1", "--oneline"])

        # 2) 发 PR：无 Token 则优雅降级，返回明确提示。
        if not self._token:
            return (
                commit_out + log_out
                + "\n[repo] 已本地提交，PR 需配置 GITHUB_TOKEN（未配置，跳过远端 PR）。"
            )
        if not self._repo:
            return (
                commit_out + log_out
                + "\n[repo] 已本地提交，但未配置 GITHUB_REPO（owner/repo），无法发 PR。"
            )

        # 惰性导入 PyGithub：只在确实要发 PR 且有 Token 时才 import，
        # 保证顶层 import 本模块不需要装 PyGithub（pip install -e . 零额外依赖）。
        try:
            from github import Github  # type: ignore
        except ImportError:
            return (
                commit_out + log_out
                + "\n[repo] 已本地提交，但 PyGithub 未安装（pip install PyGithub 后可发 PR）。"
            )

        # 真实发 PR：用 Token 建立客户端，定位 repo，基于当前分支创建 PR。
        try:
            g = Github(self._token)
            repo_obj = g.get_repo(self._repo)
            # 取默认分支作为 PR base，更通用；如需指定可扩展参数。
            base = repo_obj.default_branch
            head = branch
            pr = repo_obj.create_pull(
                title=message,
                body=f"Auto-created by DevPilot RepoSkill.\n\nCommit: {message}",
                head=head,
                base=base,
            )
            return (
                commit_out + log_out
                + f"\n[repo] PR created: {pr.html_url}"
            )
        except Exception as exc:  # noqa: BLE001 - 远端失败不阻断本地提交结果
            return (
                commit_out + log_out
                + f"\n[repo] 本地提交完成，但发 PR 失败: {exc}"
            )
