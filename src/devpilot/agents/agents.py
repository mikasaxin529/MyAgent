"""Worker 角色：Coder / Reviewer / Tester。

多 Agent 协作落地：每个角色是一个聚焦的 Agent，
通过共享黑板(Blackboard)读写中间产物。

==============================================================================
设计要点：
==============================================================================
1. 为什么所有 Worker 继承 _BaseWorker：
   - 统一持有 gateway/registry/bb/approval/audit 五大依赖，子类只重写 act()
     即可，避免每个角色重复声明依赖与构造逻辑（DRY）。
   - role 类属性便于审计/日志按角色聚合统计。

2. Blackboard 模式为什么解耦多 Agent 直接通信：
   - Coder 写 bb.code_diff，Reviewer 读 bb.code_diff，二者互不知道对方存在。
   - 新增角色（如 SecAgent 读 code_diff 写 sec_review）只需写黑板，不必改
     Coder/Reviewer，体现"对扩展开放、对修改关闭"。
   - 对比"Agent 间直接调方法 / 消息总线"：直接调用会形成网状耦合（N 个
     Agent 就有 N*N 条边），黑板把通信降为 N 条"读写黑板"边，复杂度 O(N)。

3. Orchestrator-Worker 拓扑选型理由（与 orchestrator.py 呼应）：
   - 研发闭环是顺序流水线（改码→评审→测试），适合中央编排器顺序驱动。
   - Worker 不自主决定"下一步轮到谁"，编排器统一控流转，避免 Agent 间
     互相触发导致死循环/乱序，确定性更强、更易审计。
==============================================================================
"""
from __future__ import annotations


class _BaseWorker:
    """Worker 基类：绑定 gateway + skill registry + 黑板 + 可选治理组件。

    职责：
        - 持有所有子类共需的依赖，子类只重写 act()。
        - 提供 _trace() 便捷记审计方法，统一 actor=role 与 trace_id 透传。

    为什么 approval/audit 可选且向后兼容：
        - 既有调用方可能只传 (gateway, registry, blackboard) 三参，加默认值
          None 保证不破坏旧调用。
        - 缺省时 Worker 仍能跑核心逻辑，只是不记审计/不走审批——便于最小化
          单元测试（不强制 mock 治理组件）。
    """

    role = "base"

    def __init__(self, gateway, registry, blackboard, approval=None, audit=None) -> None:
        """初始化 Worker。

        参数：
            gateway:   模型网关，Worker 用它调 LLM。
            registry:  Skill 注册中心，Worker 用它取 repo/cicd 等 Skill。
            blackboard: 共享黑板，Worker 读写阶段产物。
            approval:  审批门（可选）。Reviewer 用来对高危动作走人工审批。
            audit:     审计日志（可选）。Worker 记录自身关键动作。
        """
        self.gateway = gateway
        self.registry = registry
        self.bb = blackboard
        self.approval = approval
        self.audit = audit

    # ------------------------------------------------------------------
    # 便捷：记审计（audit 可选时安全跳过）
    # ------------------------------------------------------------------
    def _trace(self, event: str, detail: dict) -> None:
        """记一条审计事件，自动填 actor=role 与 trace_id（从黑板取）。

        为什么需要这个封装：
            - 每个 Worker 记审计都要写 actor=role、trace_id=bb.artifacts['trace_id']，
              重复且易忘；封装后子类一行调用即可，降低心智负担与漏记风险。
            - audit 为 None 时静默跳过，保证不装治理组件也能跑 Worker（可测试性）。

        参数：
            event:  事件类型，如 "tool_call"/"agent_step"。
            detail: 事件细节 dict。
        """
        if self.audit is None:
            return
        # trace_id 由编排器在 run() 里挂进黑板 artifacts；取不到则空串（兼容旧调用）。
        trace_id = self.bb.artifacts.get("trace_id", "") if self.bb else ""
        self.audit.record(event=event, actor=self.role, detail=detail, trace_id=trace_id)

    def act(self) -> None:
        """子类实现的实际动作。基类默认抛 NotImplementedError，强制子类重写。"""
        raise NotImplementedError


class CoderAgent(_BaseWorker):
    """Coder：据计划修改代码，产出 diff。

    职责：
        - 读 bb.plan（Planner 拆出的步骤），让 LLM 产出"要做的代码改动描述/伪 diff"，
          写回 bb.code_diff，供 Reviewer 评审。
        - 真实场景下应先用 registry 的 repo Skill（read_file/search_code）定位
          受影响文件，再让 LLM 据上下文产出真 diff，最终调 commit_and_pr 提交；
          demo 阶段为降低外部依赖（git/Token），产出"改动描述"即可，注释标注真改码路径。
    """

    role = "coder"

    def act(self) -> None:
        """Coder 主动作：据 plan 产出代码改动方案，写入 bb.code_diff。

        流程（注释逐步讲清）：
            1. 从黑板读 plan + task 作为上下文。
            2. （真实路径，注释标注）若 registry 有 repo Skill，可先 search_code/
               read_file 摸清现状，把命中代码片段塞进 prompt 让 LLM 改得更准。
               demo 阶段直接让 LLM 据 plan 产出改动描述。
            3. 调 gateway.chat_text 让 LLM 产出改动方案/diff。
            4. 写回 bb.code_diff；记审计。
        """
        # --- 1. 取上下文：plan 是拆解步骤，task 是原始需求。 ---
        # plan 为空时（Planner 失败降级），退回直接用 task，保证 Coder 仍有输入。
        plan_text = "\n".join(f"- {s}" for s in self.bb.plan) if self.bb.plan else "(无拆解步骤，直接依据需求)"
        task = self.bb.task

        # --- 2. 真实路径：用 repo Skill 定位受影响代码（demo 阶段注释说明）---
        # 下面这段是"真改码"应有的前置：从 registry 取 repo Skill，搜出相关文件
        # 片段拼进 prompt。demo 阶段为避免对 git/工作区的强依赖，注释保留路径，
        # 实际直接让 LLM 据 plan 产出方案。
        repo_context = ""
        repo_skill = self.registry.get("repo") if self.registry else None
        if repo_skill is not None:
            # 真实场景：用需求里的关键词搜代码，把命中行作为上下文喂给 LLM，
            # 让改动更聚焦、减少幻觉。这里做一次轻量 search，失败不影响主流程。
            try:
                # 从 task 里取一个简单关键词做搜索（demo：取第一个非停用词片段）。
                # 真实场景应由 LLM 自主决定搜什么词（走 ReAct），这里简化。
                keyword = task.split()[0] if task.split() else task
                hits = repo_skill.search_code(keyword)
                # 截断防止上下文过长；只取前若干行作为定位线索。
                repo_context = (hits or "")[:800]
                self._trace("tool_call", {"tool": "repo.search_code",
                                         "keyword": keyword,
                                         "hits_len": len(hits or "")})
            except Exception as exc:  # noqa: BLE001 - repo Skill 失败不阻断 LLM 改码
                # 搜索失败只是少了一点上下文，不该让 Coder 整体失败。
                self._trace("tool_call", {"tool": "repo.search_code", "error": repr(exc)})
                repo_context = ""

        # --- 3. 构造 prompt 调 LLM 产出改动方案 ---
        system = (
            "你是资深开发工程师。依据需求与拆解步骤，产出要做的代码改动方案。"
            "输出要求：先简述改动思路，再给出关键文件的伪 diff（用 ```diff 代码块），"
            "最后列出受影响文件。务必具体、可执行，避免空话。"
        )
        # 把 task/plan/repo_context 一起喂给 LLM：上下文越充分，改动越准。
        prompt = (
            f"## 需求\n{task}\n\n"
            f"## 拆解步骤\n{plan_text}\n\n"
        )
        if repo_context:
            prompt += f"## 仓库相关代码片段（search 命中）\n```\n{repo_context}\n```\n\n"
        prompt += "请产出代码改动方案与伪 diff。"

        # 记 LLM 调用审计（prompt 摘要，避免超长/敏感）。
        self._trace("llm_call", {"prompt_preview": prompt[:200]})

        # 真正调模型。可能抛异常，由编排器 run() 的 try/except 兜底。
        code_diff = self.gateway.chat_text(prompt, system=system)

        # --- 4. 写回黑板 + 记完成审计 ---
        self.bb.code_diff = code_diff
        self._trace("agent_step", {"step": "coder_done",
                                   "diff_len": len(code_diff)})


class ReviewerAgent(_BaseWorker):
    """Reviewer：评审 diff，标记风险点。

    关键：Reviewer 的风险标记会触发 Human-on-the-Loop 审批门。
    若 review 判定高危（如涉及线上配置/不可逆操作），调 approval.request
    让人确认是否放行 commit_and_pr。
    """

    role = "reviewer"

    def act(self) -> None:
        """Reviewer 主动作：评审 bb.code_diff，写 bb.review，必要时走审批。

        流程：
            1. 读 bb.code_diff（Coder 产物）；为空则给出"无 diff 可审"的降级评审。
            2. 调 LLM 评审：产出风险点 + 是否高危的判断。
            3. 写回 bb.review。
            4. 若 review 标记高危且注入了 approval：调 approval.request("commit_and_pr",
               {...}, reason=review) 走人工审批，把裁决结果写回黑板与审计。
        """
        # --- 1. 取被评审对象 ---
        diff = self.bb.code_diff
        if not diff:
            # Coder 失败/未产出 diff：Reviewer 仍要给出结论，而不是抛错中断。
            self.bb.review = "[reviewer] 无 code_diff 可审（Coder 阶段未产出），建议人工介入。"
            self._trace("agent_step", {"step": "reviewer_skipped_no_diff"})
            return

        # --- 2. 调 LLM 评审 ---
        system = (
            "你是资深代码评审专家。评审给定的代码改动方案/diff，输出："
            "1) 风险点列表（每条一行，含严重度 高/中/低）；"
            "2) 一行结论：是否判定为高危（含不可逆/线上影响/安全/合规问题）。"
            "格式要求：最后单独一行写 'HIGH_RISK: yes' 或 'HIGH_RISK: no'，便于程序解析。"
        )
        prompt = f"## 待评审改动\n{diff}\n\n请评审并给出风险点与高危判定。"

        self._trace("llm_call", {"prompt_preview": prompt[:200]})
        review = self.gateway.chat_text(prompt, system=system)

        # --- 3. 写回黑板 ---
        self.bb.review = review
        self._trace("agent_step", {"step": "reviewer_done", "review_len": len(review)})

        # --- 4. 解析高危判定，必要时走人工审批 ---
        # 用简单字符串匹配解析高危标记：不强制 JSON，对小模型更鲁棒。
        # 注意：把 review 整体 lower 后，与"小写常量"比较，避免大小写不一致漏判
        # （LLM 可能输出 HIGH_RISK / high_risk / High_Risk 等任意大小写）。
        high_risk = "high_risk: yes" in review.lower()

        if high_risk and self.approval is not None:
            # 高危且注入了审批门：先判断该动作是否确实需要审批（走策略表），
            # 再 request。requires_approval 是策略集中点，避免到处硬编码高危清单。
            action = "commit_and_pr"
            if self.approval.requires_approval(action):
                # 记一条审批请求审计：把动作、参数、理由（即 review）写进去。
                self._trace("approval", {"action": action,
                                         "args": {"branch": "devpilot/auto"},
                                         "reason_preview": review[:200]})
                # 阻塞式请求人工裁决。非交互环境 ApprovalGate 自身会默认拒绝。
                result = self.approval.request(
                    action=action,
                    args={"branch": "devpilot/auto", "message": self.bb.task[:200]},
                    reason=review,
                )
                # 把裁决结果写回黑板 artifacts，供下游（如 Tester/PR 提交）决策。
                self.bb.artifacts["approval"] = {
                    "approved": result.approved,
                    "comment": result.comment,
                    "modified_args": result.modified_args,
                }
                self._trace("approval", {"approved": result.approved,
                                         "comment": result.comment})
            else:
                # 策略表判定该动作非高危：跳过审批，仅记审计。
                self._trace("approval", {"action": action, "skipped": "not_required"})


class TesterAgent(_BaseWorker):
    """Tester：触发 CI、解析测试报告、反馈失败用例。

    职责：
        - 从 registry 取 cicd Skill，调 trigger_pipeline 触发流水线，
          再调 fetch_test_report 拉测试报告，写回 bb.test_result。
        - 若 cicd Skill 未配置（registry.get 返回 None）或方法抛
          NotImplementedError/任何异常，捕获并写降级提示，不中断流程。
    """

    role = "tester"

    def act(self) -> None:
        """Tester 主动作：调 cicd Skill 触发流水线并拉报告，写 bb.test_result。

        流程：
            1. 从 registry 取 cicd Skill；取不到则降级写"CI Skill 未配置"。
            2. 调 trigger_pipeline 触发流水线（高危动作，真实场景应先过审批门；
               demo 直接调，异常捕获）。
            3. 从触发返回里解析 run_id（job/queue 标识），调 fetch_test_report
               拉报告。run_id 解析失败则只保留触发结果。
            4. 把触发结果 + 测试报告拼成 bb.test_result；记审计。
        异常处理：
            任一步异常（含 NotImplementedError）都捕获写降级提示，保证流程不崩。
        """
        # --- 1. 取 cicd Skill ---
        cicd = self.registry.get("cicd") if self.registry else None
        if cicd is None:
            # Skill 未注册：降级提示而非抛错，体现"缺失时优雅降级"。
            self.bb.test_result = "[tester] CI Skill 未配置（registry 无 'cicd'），跳过测试。"
            self._trace("agent_step", {"step": "tester_skipped_no_skill"})
            return

        # --- 2. 触发流水线 ---
        # 真实场景 trigger_pipeline 属高危，应先 approval.request；这里 demo
        # 直接调，异常捕获写降级。job 名取环境变量或用默认 demo 作业名。
        import os
        job = os.getenv("DEVPILOT_CI_JOB", "devpilot-demo")
        try:
            self._trace("tool_call", {"tool": "cicd.trigger_pipeline", "job": job})
            trigger_out = cicd.trigger_pipeline(job=job, params={})
        except NotImplementedError:
            # Skill 框架要求方法返回 str，但若子类未实现会抛 NotImplementedError；
            # 捕获并写明确提示，便于说明"未配置 CI 时如何降级"。
            self.bb.test_result = (
                "[tester] cicd.trigger_pipeline 未实现（NotImplementedError），CI Skill 未配置。"
            )
            self._trace("agent_step", {"step": "tester_trigger_not_implemented"})
            return
        except Exception as exc:  # noqa: BLE001 - 兜底降级
            # 其他异常（网络/凭证/requests 未装）也降级，不崩。
            self.bb.test_result = f"[tester] trigger_pipeline 失败: {exc}"
            self._trace("agent_step", {"step": "tester_trigger_failed",
                                       "error": repr(exc)})
            return

        # --- 3. 解析 run_id 拉测试报告 ---
        # trigger_pipeline 返回形如 "...queue=http://.../queue/item/123/" 的字符串，
        # 这里做轻量解析：找 queue/ 后的 id。真实场景应根据 Location 头规范解析。
        run_id = ""
        if "queue=" in trigger_out:
            # 取 queue= 之后到行尾/空格的部分作为 run_id（格式 queue/<id>）。
            after = trigger_out.split("queue=", 1)[1].strip()
            # 去掉可能的尾部分号/换行；取首段连续非空白作为 id。
            run_id = "queue/" + after.split()[0].rstrip("/") if after else ""

        report = ""
        if run_id:
            try:
                self._trace("tool_call", {"tool": "cicd.fetch_test_report", "run_id": run_id})
                report = cicd.fetch_test_report(run_id=run_id)
            except NotImplementedError:
                report = "[tester] cicd.fetch_test_report 未实现（NotImplementedError）。"
            except Exception as exc:  # noqa: BLE001
                report = f"[tester] fetch_test_report 失败: {exc}"

        # --- 4. 拼结果写回黑板 ---
        # 触发结果 + 测试报告合并，便于下游（人/编排器）一眼看全。
        self.bb.test_result = (
            f"=== trigger ===\n{trigger_out}\n\n"
            f"=== test report (run_id={run_id or 'n/a'}) ===\n{report}"
        )
        self._trace("agent_step", {"step": "tester_done",
                                   "result_len": len(self.bb.test_result)})
