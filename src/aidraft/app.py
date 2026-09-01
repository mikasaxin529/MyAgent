"""智绘工坊 AIDraft CLI 入口：把核心模块贯通到命令行，让 demo 一键跑。

本模块把散落在各子包里的能力（模型网关、审计留痕）拧成一条用户可触达
的 CLI 命令链，每条命令都对应一块核心职责：

    chat <prompt>        —— 单轮对话，验证模型网关（路由/fallback/限流）。
    gateway-test         —— 自测：列出可用 provider 并打一次测试调用。

设计原则：
1. 惰性导入：所有可选第三方库一律在方法内 import，保证
   `import aidraft.app` 顶层不报错、`pip install -e .` 无需装额外包。
2. 每条命令都自包含地构造自己的依赖，体现"组合式架构"——
   各模块解耦，CLI 只负责装配与串联。

主入口是 Web 服务（uvicorn aidraft.web.api:app，见 web/），CLI 仅保留
网关连通性验证两条命令。
"""
from __future__ import annotations

# ---- 标准库 ----
# sys：读命令行参数、向 stderr 打印 meta 信息。
import sys

# ---- 项目内：模型网关与审计是所有命令的公共依赖 ----
# 这两个模块属于"零额外依赖"的核心层（仅依赖 openai/python-dotenv/pydantic，
# 已在 base dependencies 里），顶层导入安全。
from .gateway import build_default_gateway, ChatMessage
from .governance.audit import AuditLog


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
            ChatMessage("system", "你是智绘工坊的助手，简洁专业地回答。"),
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
# 主入口：命令分发
# ======================================================================
def main() -> int:
    """CLI 主入口：解析 argv 分发到对应命令。

    支持命令：
        aidraft chat <prompt>        单轮对话
        aidraft gateway-test         网关自测

    返回：
        进程退出码；0 成功，1 用法错误或命令内部返回非零。
    """
    args = sys.argv[1:]
    if not args:
        # 无参数：打印完整用法，列清各命令说明，便于演示。
        print("用法: aidraft <命令> [参数]")
        print("命令:")
        print("  chat <prompt>        单轮对话，验证模型网关")
        print("  gateway-test         自测：列出可用 provider 并打一次测试调用")
        return 1

    cmd = args[0]
    if cmd == "chat":
        # chat 需要一个 prompt 参数。
        if len(args) < 2:
            print("用法: aidraft chat <prompt>")
            return 1
        return cmd_chat(args[1])
    if cmd == "gateway-test":
        # gateway-test 无参数。
        return cmd_gateway_test()

    # 未知命令：列出可用命令，返回非零。
    print(f"未知命令: {cmd}")
    print("可用命令: chat, gateway-test")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
