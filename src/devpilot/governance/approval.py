"""审批门：高危动作的人工确认机制。

Human on the Loop / 人机协同治理：
    关键决策节点保留人工审核与干预，Agent 不能"一意孤行"地执行不可逆操作。

设计哲学：
    1. Agent 可以自主规划与执行常规动作（读文件、搜索、跑测试），但一旦涉及
       "不可逆 / 外部可见 / 影响线上"的动作（提 PR、触发部署、更新 Issue、发版），
       必须先过这道"审批门"——由人确认后才放行。
    2. 这就是"Human on the Loop"而非"Human in the Loop"的区别：人不在执行链路上
       阻塞每一步，而是在"关键路口"把关；日常运行仍由 Agent 自动完成，效率高。
    3. 人工不仅可以 y/n，还可以 `edit` 改写参数后放行——既保留控制权，又不丢效率。
    4. 非交互环境（CI / 后台调度 / 无 tty 的容器）下默认拒绝，宁可不做不可逆操作，
       也不在无人监督时擅自执行。这是"安全合规"的底线设计。

接入点（架构说明，方便讲解）：
    工具调用前置钩子（tool_call pre-hook）会调用 `ApprovalGate.requires_approval`
    判断动作是否高危；若是，则调用 `request()` 阻塞等待人工裁决。裁决结果同时
    写入审计日志（AuditLog），形成"决策→审批→审计→回流"闭环。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from enum import Enum


class ApprovalStatus(str, Enum):
    """审批状态枚举。

    用 str + Enum 是为了序列化时直接得到字符串（如 `"pending"`），
    方便写进审计日志 / JSON 导出，无需额外的 serializer。
    """

    PENDING = "pending"      # 等待人工裁决中（阻塞中）
    APPROVED = "approved"    # 人工已批准，可放行
    REJECTED = "rejected"    # 人工已拒绝，动作终止


@dataclass
class ApprovalRequest:
    """审批请求的数据载体。

    体现"把决策显式化"：Agent 想做什么、为什么、当前状态，全部结构化记录，
    便于审计回溯，也便于 UI 层渲染审批面板。

    属性：
        action: 动作名，如 "commit_and_pr"（提交并提 PR）、"deploy"（部署）。
        args:    动作参数，如 {"branch": "feat-x", "title": "..."}。
        reason:  为什么需要人工审核（风险评估理由），由调用方填充。
        status:  当前审批状态，默认 PENDING。
    """

    action: str          # 如 "commit_and_pr"
    args: dict           # 动作参数
    reason: str          # 为什么需要人工审核
    status: ApprovalStatus = ApprovalStatus.PENDING


@dataclass
class ApprovalResult:
    """审批裁决结果。

    体现"人可以改、不只是 yes/no"的设计：
        - approved=True 且 modified_args=None：原样放行。
        - approved=True 且 modified_args={...}：人工改写了参数后放行，调用方应使用
          改写后的参数执行（而非原始 args）。
        - approved=False：拒绝，动作终止；comment 记录拒绝理由，便于回流分析。

    属性：
        approved:       是否批准。
        comment:        人工留言（拒绝理由 / 批注），写入审计。
        modified_args: 人工改写后的参数；仅当用户选择 edit 时非 None。
    """

    approved: bool
    comment: str = ""
    modified_args: dict | None = None  # 人工可改写参数后放行


class ApprovalGate:
    """审批门：高危动作执行前的人工确认关卡。

    关键决策保留人工审核与干预。

    工作流：
        1. 工具层在执行前调用 `requires_approval(action)` 判断是否高危。
        2. 若高危，调用 `request(action, args, reason)` 进入阻塞式确认。
        3. 人工在 CLI 输入 y/n/edit，得到 `ApprovalResult`。
        4. 调用方依据结果决定是否执行、是否使用改写后的参数。
        5. （外部）将本次审批请求与裁决写入 AuditLog，完成可追溯闭环。

    非交互安全策略：
        检测 `sys.stdin.isatty()` 与 `sys.stdout.isatty()`，只要任一为 False
        （如 CI、后台、管道输入、容器无 tty），一律默认拒绝。理由是：
        不可逆操作在无人可交互时不应自动执行，宁可漏做不可错做。
    """

    # 高危动作集合。判定逻辑就一条：`action in HIGH_RISK_ACTIONS`。
    # 之所以用集合 + 命名常量，而不是散落的 if-else，是为了让"风险策略"集中可见、
    # 易扩展（新增高危动作只改一处），也便于一行讲清"我们怎么定义高危"。
    #
    # 高危的判定标准（为何这几个算高危）：
    #   - commit_and_pr:   不可逆地改动远端仓库、对外可见（PR 会通知 reviewer）。
    #   - trigger_pipeline: 触发 CI/CD，可能部署到线上、消耗共享资源。
    #   - update_issue:    修改外部 Issue Tracker（GitHub Issue / Jira），对外可见。
    #   - deploy:          直接部署，线上影响面最大、回滚成本最高。
    # 这些动作的共同点：不可逆 / 外部可见 / 影响他人。读文件、跑本地测试不算高危。
    HIGH_RISK_ACTIONS = {"commit_and_pr", "trigger_pipeline", "update_issue", "deploy"}

    def requires_approval(self, action: str) -> bool:
        """判断某动作是否必须经过人工审批。

        策略化地决定"哪些动作必须审批"。

        设计：O(1) 集合判定。调用方（工具层 pre-hook）据此决定是否进入阻塞确认。
        低危动作（读、查、本地跑测试）直接放行，避免把人拖进每个琐碎决策——
        这是"Human on the Loop"效率优于"in the Loop"的关键。

        参数：
            action: 动作名。
        返回：
            True 表示该动作高危，必须先过审批门；False 表示可自动放行。
        """
        # 集合成员判定，O(1)，且语义清晰：高危即"在白名单集合里"。
        return action in self.HIGH_RISK_ACTIONS

    def request(self, action: str, args: dict, reason: str = "") -> ApprovalResult:
        """发起审批请求，阻塞等待人工裁决。

        关键决策节点保留人工审核与干预。

        交互流程（CLI）：
            1. 打印动作、参数、风险理由——把"Agent 要做什么、为什么危险"摊到桌面，
               让人能在充分信息下决策，而不是盲签。
            2. 用 input() 询问 [y/n/edit]：
                - y    -> 批准，原样放行（ApprovalResult(True)）。
                - n    -> 拒绝，并要求输入拒绝理由（ApprovalResult(False, comment)）。
                - edit -> 让用户输入一段 JSON 改写 args，批准但使用改写后的参数
                          （ApprovalResult(True, modified_args=...)）。这给了人
                          "纠正而非否决"的能力，适合参数小错的场景。
            3. 输入异常（非法选项 / JSON 解析失败）会重新询问，避免误操作。

        非交互环境（无 tty）：
            直接返回拒绝，comment 注明原因。体现"安全合规"：宁可不做不可逆动作，
            也不在无人监督时擅自执行。

        参数：
            action: 动作名。
            args:   原始动作参数（可能被人工改写）。
            reason: 风险理由，展示给人看。
        返回：
            ApprovalResult，调用方据此决定执行 / 终止 / 用改写参数执行。
        """
        # --- 安全合规第一：先判交互可用性 ---
        # 任一端不是 tty，就认为是非交互环境（CI / 后台 / 管道 / 容器）。
        # isatty() 在被管道喂入、CI runner、nohup 后台等场景会返回 False。
        if not self._is_interactive():
            # 默认拒绝：不可逆操作在无人交互时不执行，是"安全合规"底线。
            # 同时把拒绝原因结构化返回，便于上游记录审计、提示用户改用交互模式。
            return ApprovalResult(
                approved=False,
                comment="非交互环境，默认拒绝以保安全",
            )

        # --- 把决策信息摊到桌面，让人在充分信息下判断 ---
        # 用 print（而非 logging）是因为审批是面向"人此刻在场"的交互，应直达终端。
        print()
        print("=" * 60)
        print("[审批门] 高危动作待确认")
        print("-" * 60)
        print(f"  动作(action) : {action}")
        # args 用 JSON 美化输出，保证可读性；ensure_ascii=False 让中文正常显示。
        print(f"  参数(args)   : {json.dumps(args, ensure_ascii=False, indent=2)}")
        print(f"  风险理由     : {reason or '(未提供)'}")
        print("-" * 60)
        print("请选择: [y] 批准 / [n] 拒绝 / [edit] 改写参数后批准")

        # --- 阻塞循环：直到拿到一个合法裁决 ---
        # 用 while 是因为人可能输错（非法选项 / 畸形 JSON），要给重试机会而非直接崩。
        while True:
            choice = input("审批 [y/n/edit]: ").strip().lower()

            # --- 拒绝分支：要留理由，便于事后复盘为什么没做 ---
            if choice in ("n", "no"):
                comment = input("拒绝理由(可选, 回车跳过): ").strip()
                return ApprovalResult(approved=False, comment=comment or "人工拒绝")

            # --- 批准分支：原样放行 ---
            if choice in ("y", "yes"):
                return ApprovalResult(approved=True, comment="人工批准")

            # --- 改写分支：人输入新 JSON 参数后批准 ---
            # 适用场景：参数方向对但有笔误（如分支名、标题），改一下就能放行，
            # 不必否决重来。这显著降低"小错导致全流程回退"的摩擦。
            if choice == "edit":
                raw = input("输入改写后的参数 JSON: ").strip()
                try:
                    # 严格用 JSON 解析，避免 eval 类不安全方案。
                    modified = json.loads(raw)
                except json.JSONDecodeError as exc:
                    # JSON 畸形：提示并重新询问，不直接失败——人可能只是少了个逗号。
                    print(f"[解析失败] 不是合法 JSON: {exc}，请重试。")
                    continue
                # 改写结果必须是 dict（动作参数始终是 dict），否则语义错乱。
                if not isinstance(modified, dict):
                    print("[解析失败] 顶层必须是 JSON 对象(字典)，请重试。")
                    continue
                return ApprovalResult(
                    approved=True,
                    comment="人工改写参数后批准",
                    modified_args=modified,
                )

            # 非法选项：提示后继续循环，避免误触导致流程中断。
            print("[未识别] 请输入 y / n / edit 之一。")

    @staticmethod
    def _is_interactive() -> bool:
        """检测当前是否处于可交互环境。

        判定标准：标准输入与标准输出都是 tty（终端）。
        任一不是，即视为非交互：
            - stdin 非 tty：说明输入被管道/文件喂入，input() 拿不到真人键盘输入。
            - stdout 非 tty：说明输出被重定向到文件/管道，看不到提示也就没法审批。
        这是"宁可保守拒绝"的体现：在拿不准有没有人的时候，默认没人。

        返回：
            True 表示可以进行 input() 交互；False 表示应走默认拒绝路径。
        """
        # 双端都要是 tty 才算可交互；用短路避免在缺端时抛异常。
        try:
            stdin_tty = sys.stdin.isatty()
            stdout_tty = sys.stdout.isatty()
        except Exception:
            # 某些异常环境（如被劫持的 stream）isatty() 自身可能抛异常，
            # 一律按"非交互"处理，保守优先。
            return False
        return stdin_tty and stdout_tty
