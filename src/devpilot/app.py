"""DevPilot CLI 入口：把所有模块贯通到命令行，让 demo 一键跑。

本模块是 DevPilot 的"指挥台"。它把散落在各子包里的能力
（模型网关、Multi-Agent 编排、MCP Skill 生态、评估体系、人机协同治理）
拧成一条用户可触达的 CLI 命令链，每条命令都对应一块核心职责：

    chat <prompt>        —— 单轮对话，验证模型网关（路由/fallback/限流）。
    gateway-test         —— 自测：列出可用 provider 并打一次测试调用。
    run <task>           —— 跑完整 Multi-Agent 流程（规划→改码→评审→测试→审批）。
    eval                 —— 跑 Evaluation Harness：Golden 集 + LLM-judge + 多维度指标。
    skills               —— 列出已注册 Skill 及其 specs，验证 MCP Skill 生态。

设计原则：
1. 惰性导入：所有可选第三方库（PyGithub/requests 等）一律在方法内 import，
   保证 `import devpilot.app` 顶层不报错、`pip install -e .` 无需装额外包。
2. 凭证从环境变量读，缺失时优雅降级：返回明确提示而非崩溃（见 _build_registry）。
3. 每条命令都自包含地构造自己的依赖（gateway/registry/audit/approval），
   体现"组合式架构"——各模块解耦，CLI 只负责装配与串联。
4. 保留现有 chat/gateway-test 的公开接口签名不变，仅补注释 + 新增命令。
"""
from __future__ import annotations

# ---- 标准库 ----
# sys：读命令行参数、向 stderr 打印 meta 信息。
# os：读环境变量（Skill 凭证可选）。
# time：eval 命令里给 stub agent_run_fn 记延迟。
import sys
import os
import time

# ---- 项目内：模型网关与审计是几乎所有命令的公共依赖 ----
# 这两个模块属于"零额外依赖"的核心层（仅依赖 openai/python-dotenv/pydantic，
# 已在 base dependencies 里），顶层导入安全。
from .gateway import build_default_gateway, ChatMessage
from .governance.audit import AuditLog


# ======================================================================
# 公共：构造 SkillRegistry —— 把"内部系统"标准化为可复用 Skill 的注册中心
# ======================================================================
def _build_registry():
    """构造并装配默认 SkillRegistry，注册 Repo/CICD/Issue 三个 Skill。

    通过 MCP/A2A 协议把内部系统封装为标准化 AI Skills。
    这条命令链是"低代码 Skill 框架"的装配点——新增一个系统只需在此 register。

    凭证策略（硬性要求：从环境变量读，缺失优雅降级）：
        - RepoSkill：GITHUB_TOKEN / GITHUB_REPO 缺失时退化为"仅本地 git"，不崩溃。
        - CICDSkill：JENKINS_URL/JENKINS_USER/JENKINS_TOKEN 缺失时方法返回降级提示。
        - IssueSkill：JIRA_URL/JIRA_USER/JIRA_TOKEN 缺失时方法返回降级提示。
        所有 Skill 的构造函数本身都不因凭证缺失而抛错，降级发生在真正调用时，
        因此即便无任何凭证，`skills` / `run` 命令也能完整展示骨架行为。

    返回：
        SkillRegistry 实例，已注册 repo/cicd/issue 三个 Skill。
    """
    # 局部 import：Skill 类定义在 skills 子包，惰性加载避免顶层 import 时
    # 触发子模块（部分含可选依赖的 Skill 模块）的不必要加载。
    from .skills.registry import SkillRegistry
    from .skills.repo_skill import RepoSkill
    from .skills.cicd_skill import CICDSkill
    from .skills.issue_skill import IssueSkill

    registry = SkillRegistry()

    # RepoSkill：token/repo 留空 → 内部会从环境变量读，读不到就降级为仅本地 git。
    # repo_path 用当前工作目录，便于 demo（用户在仓库根目录跑 devpilot）。
    registry.register(RepoSkill(repo_path=os.getcwd()))

    # CICDSkill / IssueSkill：构造时不传参，让其内部从环境变量读凭证。
    # 这样 CLI 层不硬编码任何凭证，符合"凭证一律从环境变量读"。
    registry.register(CICDSkill())
    registry.register(IssueSkill())

    return registry


# ======================================================================
# 命令：chat —— 单轮对话，验证模型网关
# ======================================================================
def cmd_chat(prompt: str) -> int:
    """单轮对话命令：把一条 prompt 经网关送给 LLM，打印回复与 meta。

    模型网关是"Agent 与具体模型之间唯一的边界"，
    本命令用最小用例验证网关的路由 / fallback / 限流 / token 计费是否就绪。

    流程：
        1. build_default_gateway()：按 settings 注册所有已配置 provider。
        2. AuditLog 记一条 llm_call 审计（trace_id="cli"），演示"调用即留痕"。
        3. gw.chat(...)：带 system prompt 调一轮，返回 ChatResponse。
        4. 打印回复正文到 stdout，meta（provider/model/latency/tokens）到 stderr，
           分流便于脚本只取正文而过滤 meta。

    参数：
        prompt: 用户输入的对话文本。
    返回：
        0 表示成功；网关抛错会冒泡到 main() 由其兜底打印。
    """
    # 构造网关：内部会校验至少有一个 provider 配了 API Key，否则抛友好提示。
    gw = build_default_gateway()
    # 审计日志：即使是最简单的单轮对话也记一条，体现"调用即审计"的底线。
    audit = AuditLog()
    audit.record("llm_call", "user", {"prompt": prompt}, trace_id="cli")

    # 调模型：system 用一条 system 消息注入（网关 chat 不接受 system 关键字，
    # 统一通过消息列表表达 system/user/assistant 角色）。
    resp = gw.chat(
        [
            ChatMessage("system", "你是 DevPilot 的助手，简洁专业地回答。"),
            ChatMessage("user", prompt),
        ],
    )
    # 正文给 stdout（便于管道消费）。
    print(resp.content)
    # meta 给 stderr：provider（用了哪家）、model、延迟、token 用量。
    # 这些正是"稳定性、响应效率、成本"的可观测信号。
    print(
        f"\n[meta] provider={resp.provider} model={resp.model} "
        f"latency={resp.latency_ms}ms tokens={resp.prompt_tokens}+{resp.completion_tokens}",
        file=sys.stderr,
    )
    return 0


# ======================================================================
# 命令：gateway-test —— 自测：列出可用 provider 并打一次测试调用
# ======================================================================
def cmd_gateway_test() -> int:
    """网关自测命令：展示可用 provider、主备模型与 fallback 链，并打一次最小调用。

    网关的"路由 / fallback"是否就绪，一眼可见。
    本命令是 demo 与演示时最先跑的 sanity check。

    输出：
        - 可用 providers 列表
        - 主模型 / 备模型与 pick_chain（实际会尝试的顺序链）
        - 一次最小测试调用的返回内容与 latency
    """
    gw = build_default_gateway()
    # available_providers：只列出真正配了 API Key 的，便于确认环境是否就绪。
    print("可用 providers:", gw.available_providers)
    # _primary/_fallback 是路由策略；_pick_chain 是 fallback 链（主→备，过滤不可用）。
    print("主模型 / 备模型:", gw._primary, "/", gw._fallback, "(链:", gw._pick_chain(), ")")
    # 最小调用：用 gw.chat 拿完整 ChatResponse（含 provider/latency），便于展示可观测信息。
    resp = gw.chat(
        [
            ChatMessage("system", "只回复'在的'两个字。"),
            ChatMessage("user", "回复两个字：在的"),
        ],
    )
    print("测试调用返回:", resp.content)
    print(f"[meta] provider={resp.provider} latency={resp.latency_ms}ms")
    return 0


# ======================================================================
# 命令：run <task> —— 跑完整 Multi-Agent 流程
# 多 Agent 协作 + Skill 调用 + 人机协同治理
# ======================================================================
def cmd_run(task: str) -> int:
    """跑完整 Multi-Agent 研发闭环：规划→改码→评审→测试→（高危则审批）。

    多 Agent 协作：Planner/Coder/Reviewer/Tester 编排。
    Worker 通过 SkillRegistry 调用 repo/cicd 等 Skill。
    高危动作走 ApprovalGate，全程 AuditLog 留痕。

    装配流程（组合式架构的体现）：
        1. build_default_gateway() —— LLM 边界。
        2. _build_registry()      —— 注册 Repo/CICD/Issue Skill（凭证缺失优雅降级）。
        3. AuditLog()             —— 审计收集器。
        4. ApprovalGate()         —— 高危动作审批门。
        5. Orchestrator(gateway, registry, audit, approval) —— 把上述依赖串成流水线。
        6. orchestrator.run(task) —— 真正跑，返回填满各阶段产物的 Blackboard。
        7. 打印 Blackboard 各字段 + 审计条目数，作为可观测输出。

    参数：
        task: 用户的研发需求文本，如"给 FastAPI 加一个 /health 健康检查接口"。
    返回：
        0 表示流程跑完（即便某 Worker 失败也算跑完，因为编排器韧性兜底）。
    """
    # 局部 import：Orchestrator 与 ApprovalGate 属于"流程层"，惰性导入避免顶层耦合。
    from .agents.orchestrator import Orchestrator
    from .governance.approval import ApprovalGate

    # ---- 1. 装配依赖 ----
    # build_default_gateway 在没有任何 provider 配 API Key 时会抛 RuntimeError；
    # 这里捕获并打印明确提示，体现"凭证缺失优雅降级"而非崩溃。
    try:
        gw = build_default_gateway()          # 模型网关
    except RuntimeError as exc:
        print(f"[run] 模型网关不可用: {exc}")
        print("[run] 请在 .env 中至少配置一个 LLM API Key 后重试。")
        return 1
    registry = _build_registry()           # Skill 生态
    audit = AuditLog()                     # 审计日志
    approval = ApprovalGate()              # 审批门

    # ---- 2. 构造编排器：把所有依赖注入进去 ----
    # 编排器只负责"拆任务、派活、收结果、过审批、记审计"，本身不做具体研发动作。
    orchestrator = Orchestrator(gw, registry, audit=audit, approval=approval)

    # ---- 3. 跑全流程，拿到填满产物的黑板 ----
    bb = orchestrator.run(task)

    # ---- 4. 打印 Blackboard 各字段：一次 run 的所有阶段产物一目了然 ----
    # 这就是 Blackboard 模式的收益：从一块黑板即可复盘整次运行。
    print("\n" + "=" * 60)
    print("[run] Multi-Agent 流程完成")
    print("=" * 60)
    print(f"task        : {bb.task}")
    # plan 是列表，逐行打印便于阅读。
    print(f"plan        : ({len(bb.plan)} 步)")
    for i, step in enumerate(bb.plan, 1):
        print(f"  {i}. {step}")
    # code_diff / review / test_result 可能很长，截断预览，避免刷屏。
    # 这里直接对黑板字段做截断展示，不依赖 Blackboard 自身方法，保持解耦。
    print(f"code_diff   : {_truncate_preview(bb.code_diff)}")
    print(f"review      : {_truncate_preview(bb.review)}")
    print(f"test_result : {_truncate_preview(bb.test_result)}")
    # artifacts 是自由扩展字段，可能含 trace_id / approval 裁决等。
    print(f"artifacts   : {bb.artifacts}")

    # ---- 5. 打印审计条目数 + 按事件类型聚合的摘要 ----
    # to_summary() 返回 {event: count}，一眼看出"LLM 调了几次、工具用了几次、人介入几次"。
    entries = audit.entries()
    print("\n[audit] 条目数:", len(entries))
    print("[audit] 摘要  :", audit.to_summary())
    return 0


# ======================================================================
# 命令：eval —— 跑 Evaluation Harness
# 评估体系 + 自动化评测流水线
# ======================================================================
def cmd_eval() -> int:
    """跑 Evaluation Harness：Golden 集 + LLM-judge + 多维度指标聚合。

    多维度评测基准（准确性/鲁棒性/任务完成率/延迟/成本）。
    自动化评测流水线 + 数据飞轮。

    流程：
        1. load eval_data/golden.jsonl 到 GoldenSet。
        2. 构造 LLMJudge(gateway) —— 用网关里配置的模型当"阅卷老师"。
        3. agent_run_fn 用一个简化 stub：对每条 task 调 gateway.chat_text 返回，
           并记 latency。注释说明真实场景应跑完整 Orchestrator。
        4. 调 run_evaluation(golden_set, judge, agent_run_fn) 拿 Metrics。
        5. 打印 Metrics.to_dict()。

    为什么 agent_run_fn 是 stub：
        评测流水线的核心是"被测 Agent 与评测器解耦"——agent_run_fn 是一个 callable，
        真实场景下应传入 orchestrator.run 的封装（跑完返回最终输出）；
        这里为 demo 可一键跑、不依赖外部凭证齐全，用一个最小 stub 代替。
        stub 的存在恰恰证明了评测框架的解耦设计：换掉 stub 即可评测真实 Agent。

    返回：
        0 表示评测跑完（即便某条 case 评测失败也算跑完，metrics 流水线韧性兜底）。
    """
    # 局部 import：评估三件套惰性加载，避免顶层依赖。
    from .eval.dataset import GoldenSet
    from .eval.judge import LLMJudge
    from .eval.metrics import run_evaluation

    # ---- 1. 加载 Golden 集 ----
    # 路径相对项目根目录的 eval_data/golden.jsonl。用相对路径 + 找项目根，
    # 避免 cwd 不在仓库根时找不到文件。
    golden_path = _resolve_eval_path("eval_data/golden.jsonl")
    golden_set = GoldenSet()
    try:
        golden_set.load_jsonl(golden_path)
    except FileNotFoundError:
        # 评测集缺失：给明确提示而非崩溃，体现"优雅降级"。
        print(f"[eval] 找不到评测集: {golden_path}")
        print("[eval] 请确认项目根目录下存在 eval_data/golden.jsonl。")
        return 1

    cases = golden_set.cases()
    if not cases:
        print("[eval] 评测集为空，无 case 可跑。")
        return 0
    print(f"[eval] 加载 {len(cases)} 条 Golden case。")

    # ---- 2. 构造网关 + LLM-judge ----
    # judge 复用网关的 fallback/限流/缓存，provider 挂了自动切备模型。
    # 网关在无任何 provider 配 Key 时抛 RuntimeError，这里优雅降级提示。
    try:
        gw = build_default_gateway()
    except RuntimeError as exc:
        print(f"[eval] 模型网关不可用: {exc}")
        print("[eval] 评测需要 LLM-judge，请在 .env 中至少配置一个 LLM API Key 后重试。")
        return 1
    judge = LLMJudge(gw)

    # ---- 3. agent_run_fn：被测 Agent 的运行入口（此处为简化 stub） ----
    # 签名约束：callable(task:str) -> (output:str, latency_ms:float, tokens:int)
    # 真实场景应改为：
    #     def agent_run_fn(task):
    #         bb = orchestrator.run(task)
    #         return bb.code_diff, latency, tokens
    # 这里用最小 stub 让 demo 一键可跑，且不依赖外部 Skill 凭证齐全。
    def agent_run_fn(task: str):
        # 记开始时间，算端到端延迟（SLA 维度）。
        t0 = time.time()
        # 直接调网关对话：用 task 当 prompt，让模型给一个"改动描述"作为 output。
        # 这模拟"Agent 跑完一轮产出了输出"，供 judge 评分。
        output = gw.chat_text(task, system="你是研发助手，针对需求给出简短的改动方案。")
        latency_ms = (time.time() - t0) * 1000.0
        # tokens 简化为 0：stub 不强求精确计费，真实 Agent 应从 ChatResponse 取。
        return output, latency_ms, 0

    # ---- 4. 跑全量评测 ----
    # run_evaluation 内部已做单条 case 异常隔离，不会因某条崩而中断全量。
    metrics = run_evaluation(golden_set, judge, agent_run_fn)

    # ---- 5. 打印多维度指标 ----
    print("\n" + "=" * 60)
    print("[eval] 评测完成，多维度指标：")
    print("=" * 60)
    _print_metrics(metrics.to_dict())
    return 0


def _truncate_preview(text: str, limit: int = 600) -> str:
    """把长文本截断为可一行预览的短串，避免刷屏。

    用于 Blackboard 字段（code_diff/review/test_result）展示。
    空文本返回占位符；超长则截断并标注字符数。

    参数：
        text:  原始文本。
        limit: 预览长度上限。
    返回：
        截断后的预览串。
    """
    if not text:
        return "(空)"
    if len(text) <= limit:
        return text
    return text[:limit] + f" ...(共 {len(text)} 字符，已截断)"


def _resolve_eval_path(rel: str) -> str:
    """把相对项目根的路径解析为绝对路径。

    为什么需要它：`python -m devpilot.app` 的 cwd 可能是任意目录，
    而 eval_data 相对项目根。这里用本文件位置向上找项目根（含 pyproject.toml），
    再拼 rel，保证无论从哪跑都能定位到评测集。

    参数：
        rel: 相对项目根的路径，如 "eval_data/golden.jsonl"。
    返回：
        绝对路径字符串。
    """
    from pathlib import Path
    # 本文件位于 src/devpilot/app.py，项目根是向上两级再上一级（src 的父目录）。
    # 即 app.py -> devpilot -> src -> 项目根。
    here = Path(__file__).resolve()
    root = here.parents[2]  # src/devpilot/app.py -> parents[2] = 项目根
    return str(root / rel)


def _print_metrics(d: dict) -> None:
    """友好打印 Metrics dict，把每个维度单独成行便于阅读。

    参数：
        d: Metrics.to_dict() 的返回。
    """
    # 顶层标量维度：准确性/鲁棒性/完成率/延迟/成本。
    print(f"  accuracy            : {d.get('accuracy', 0.0):.3f}   # 整体质量基线")
    print(f"  robustness          : {d.get('robustness', 0.0):.3f}   # 对抗/边界 case 退化度")
    print(f"  task_completion_rate: {d.get('task_completion_rate', 0.0):.3f}   # 业务侧：多少比例真正做完")
    print(f"  avg_latency_ms      : {d.get('avg_latency_ms', 0.0):.1f}   # SLA 视角")
    print(f"  avg_token_cost      : {d.get('avg_token_cost', 0.0):.1f}   # 成本视角")
    # per_tag：按标签分维度，定位薄弱环节，驱动数据飞轮。
    per_tag = d.get("per_tag", {})
    if per_tag:
        print("  per_tag             :")
        for tag, info in per_tag.items():
            acc = info.get("accuracy", 0.0)
            cnt = info.get("count", 0)
            print(f"    {tag:20s} accuracy={acc:.3f} count={cnt}")


# ======================================================================
# 命令：skills —— 列出已注册 Skill 及其 specs
# 低代码 Skill 框架与可复用技能生态
# ======================================================================
def cmd_skills() -> int:
    """列出已注册 Skill 及其 specs（能力清单），验证 MCP Skill 生态。

    把内部系统（代码仓库、CI/CD、项目管理）封装为标准化 AI Skills。
    本命令是"低代码 Skill 框架"的可观测出口——一眼看到生态里有哪些 Skill、
    每个 Skill 暴露了哪些 MCP tool（name/description/schema）。

    输出：
        - 每个 Skill 的 name
        - 该 Skill 的 specs 列表：name / description / schema
    设计要点：
        specs() 返回的列表即 MCP Server 的 tools 列表；registry.all_specs() 聚合后
        可一次性注册成 MCP Server，或通过 A2A 协议广播给其他 Agent。
    """
    registry = _build_registry()
    print("\n" + "=" * 60)
    print("[skills] 已注册 Skill 生态")
    print("=" * 60)
    for name in registry.list_skills():
        skill = registry.get(name)
        if skill is None:
            continue
        print(f"\n■ Skill: {name}")
        specs = skill.specs()
        if not specs:
            print("  (无 specs)")
            continue
        for spec in specs:
            # 每个 spec 对应一个 MCP tool：name/description/schema。
            print(f"  - {spec.name}: {spec.description}")
            # schema 打印键名即可，避免刷屏。
            schema_keys = list(spec.schema.keys()) if isinstance(spec.schema, dict) else []
            print(f"    schema: {schema_keys}")
    # all_specs() 聚合视图：便于"一次性注册成 MCP Server"。
    print(f"\n[skills] 聚合能力数 (all_specs): {len(registry.all_specs())}")
    return 0


# ======================================================================
# 主入口：命令分发
# ======================================================================
def main() -> int:
    """CLI 主入口：解析 argv 分发到对应命令。

    支持命令（保留 chat/gateway-test，新增 run/eval/skills）：
        devpilot chat <prompt>        单轮对话
        devpilot gateway-test         网关自测
        devpilot run <task>           完整 Multi-Agent 流程
        devpilot eval                 评测流水线
        devpilot skills               列出 Skill 生态

    返回：
        进程退出码；0 成功，1 用法错误或命令内部返回非零。
    """
    args = sys.argv[1:]
    if not args:
        # 无参数：打印完整用法，列清各命令说明，便于演示。
        print("用法: devpilot <命令> [参数]")
        print("命令:")
        print("  chat <prompt>        单轮对话，验证模型网关")
        print("  gateway-test         自测：列出可用 provider 并打一次测试调用")
        print("  run <task>           跑完整 Multi-Agent 流程")
        print("  eval                 跑 Evaluation Harness")
        print("  skills               列出已注册 Skill 及其 specs")
        return 1

    cmd = args[0]
    if cmd == "chat":
        # chat 需要一个 prompt 参数。
        if len(args) < 2:
            print("用法: devpilot chat <prompt>")
            return 1
        return cmd_chat(args[1])
    if cmd == "gateway-test":
        # gateway-test 无参数。
        return cmd_gateway_test()
    if cmd == "run":
        # run 需要一个 task 参数；支持 task 含空格（用引号包裹）。
        if len(args) < 2:
            print("用法: devpilot run <task>")
            return 1
        # 用 " ".join 把后续参数拼回完整 task，便于用户不加引号也能输入多词需求。
        return cmd_run(" ".join(args[1:]))
    if cmd == "eval":
        # eval 无参数；评测集路径在内部解析。
        return cmd_eval()
    if cmd == "skills":
        # skills 无参数。
        return cmd_skills()

    # 未知命令：列出可用命令，返回非零。
    print(f"未知命令: {cmd}")
    print("可用命令: chat, gateway-test, run, eval, skills")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
