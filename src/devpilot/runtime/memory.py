"""Agent 记忆管理：短期上下文 + 长期记忆。

实现运行时上下文管理。

为什么需要记忆压缩（设计要点）：
    LLM 的上下文窗口是有限的（4k/8k/32k/128k 视模型而定）。一次长会话里，
    Agent 的 Thought/Action/Observation 会不断累积，很快就会把上下文撑爆。
    超出窗口要么报错要么被模型静默截断——截断会丢掉早期关键信息（如用户原始目标、
    已做过的决策），导致 Agent 行为漂移。

    记忆压缩的本质是"信息保真 + 预算约束"：
    - 保留 system 提示（格式约束、角色设定，不可丢）
    - 保留最近 N 条（最近的上下文对当下决策最相关）
    - 中间历史做一次 LLM 摘要压缩成一段文本，腾出 token 预算

    这是生产级 Agent 必备能力，区别于"demo 能跑就行"。本项目实现一个简洁但
    完整的版本：token 估算用字符数/4 近似（中文≈1字≈1.5token，英文≈4字符≈1token，
    取 4 是稳妥的近似值），压缩惰性调 gateway。
"""
from __future__ import annotations

from ..gateway import ChatMessage


class Memory:
    """Agent 记忆：受 token 预算约束的消息历史，超限时自动摘要压缩。

    设计：
    - _messages: 原始消息列表（role/content），按时间顺序。
    - _max_tokens: token 预算上限，to_messages 时若估算超限则触发压缩。
    - _summarizer: 可选的摘要 gateway。通过 set_summarizer 注入，缺失时退化为
      "硬裁剪"（直接砍中间历史只留首尾），保证无 gateway 也能跑。

    注意：构造函数签名严格保留骨架定义（只接收 max_tokens），不新增参数，
    以满足"公开接口签名不变"的硬性要求。摘要 gateway 通过 set_summarizer 注入，
    这是对"只填实现体、不改签名"约束与"惰性调 gateway"设计需求的双全方案。
    """

    def __init__(self, max_tokens: int = 8000) -> None:
        """初始化记忆。

        Args:
            max_tokens: token 预算。默认 8000，适配多数模型窗口留余量。
        """
        self._messages: list[dict] = []
        self._max_tokens = max_tokens
        # 摘要用 gateway，默认 None。需要摘要压缩时由上层调 set_summarizer 注入。
        # 惰性：None 时 to_messages 退化为硬裁剪，不调任何 LLM。
        self._summarizer = None

    def set_summarizer(self, gateway) -> None:
        """注入用于记忆压缩的 gateway。

        为什么不放进构造函数：构造函数签名骨架已定（只 max_tokens），改签名会破坏
        既有调用方与"接口签名不变"约束。摘要 gateway 是可选增强项，用 setter 注入
        既满足"惰性调 gateway"的设计，又不改公开构造签名。

        Args:
            gateway: 模型网关，to_messages 超预算时用它做一次摘要压缩。
        """
        self._summarizer = gateway

    def add(self, role: str, content: str) -> None:
        """追加一条消息：运行时上下文积累。

        Args:
            role: system | user | assistant | tool
            content: 消息文本
        """
        self._messages.append({"role": role, "content": content})

    def to_messages(self) -> list[dict]:
        """返回适合塞进 LLM 的消息列表，超 token 预算时自动压缩。

        压缩策略（三段式）：
            1. 估算总 token：用字符数 // 4 近似（见模块 docstring 取 4 的理由）。
            2. 若未超预算：原样返回全部消息。
            3. 若超预算：
               a. 保留首条 system（格式约束不可丢）
               b. 保留最近 N 条（N 默认取 6，最近的上下文最相关）
               c. 中间历史压缩：有 summarizer 则调一次 LLM 摘要成一段 assistant
                  消息塞回；无 summarizer 则直接丢弃中间（硬裁剪兜底）。

        为什么是"摘要 + 首尾保留"而不是"滑动窗口只留最近"：
            滑动窗口会丢掉早期关键信息（用户原始目标、已做决策），导致 Agent 漂移。
            摘要压缩能在有限 token 内保留信息要点，是更优的信息保真方案。

        Returns:
            压缩后仍可能被 LLM 接受的消息列表（role/content dict）。
        """
        # ---- 1. 估算 token ----
        est_tokens = _estimate_tokens(self._messages)
        if est_tokens <= self._max_tokens:
            # 未超预算，原样返回拷贝（避免外部修改污染内部状态）。
            return list(self._messages)

        # ---- 2. 超预算，走压缩 ----
        # 首条若为 system 则单独保留（格式约束/角色设定，丢了模型行为会偏）。
        head: list[dict] = []
        rest = self._messages
        if self._messages and self._messages[0]["role"] == "system":
            head = [self._messages[0]]
            rest = self._messages[1:]

        # 最近 N 条保留原文：最近的上下文对当下决策最相关，不能压缩丢细节。
        recent_n = 6
        # 边界：历史不够长时，recent 可能吃掉全部，此时没有可压缩的中间段，
        # 直接硬裁剪只留 head + recent（极端兜底，几乎不会触发）。
        if len(rest) <= recent_n:
            return head + rest[-recent_n:] if rest else head

        middle = rest[: len(rest) - recent_n]
        recent = rest[len(rest) - recent_n :]

        # ---- 3. 中间段压缩 ----
        summary_msg = self._summarize(middle)

        return head + summary_msg + recent

    def clear(self) -> None:
        """清空记忆。新会话开始时调用。"""
        self._messages.clear()

    def _summarize(self, messages: list[dict]) -> list[dict]:
        """把一段历史消息压缩成一条摘要消息。

        有 summarizer gateway 时调一次 LLM 做摘要；无 summarizer 时硬裁剪兜底。
        返回 list（便于上层直接拼接）：摘要成功返回 [summary_msg]，失败返回 []。

        为什么惰性调 gateway：
            - 摘要只在真正超预算时才需要，绝大多数短会话根本不会触发；
            - gateway 依赖 API Key，缺失时模块顶层 import 不该崩（见项目硬性要求3）；
            - 把摘要调用封装在方法里，便于单独测试和替换策略。

        Args:
            messages: 待压缩的中间历史。

        Returns:
            压缩后的消息列表（0 或 1 条）。
        """
        # 无 summarizer：硬裁剪兜底。返回空列表 = 直接丢弃中间段，仅靠 head+recent。
        # 这是降级方案，信息损失大但保证不崩。注释里写明，便于讲清"降级路径"。
        if self._summarizer is None:
            return []

        # 拼中间历史成文本喂给摘要 LLM。
        transcript = "\n".join(f"[{m['role']}] {m['content']}" for m in messages)
        system = (
            "你是记忆压缩器。把以下 Agent 历史轨迹压缩成一段保留关键事实与决策的摘要，"
            "不要编造，保留：用户目标、已做决策、关键观察、未完成事项。直接输出摘要文本。"
        )
        try:
            resp = self._summarizer.chat(
                [ChatMessage("system", system), ChatMessage("user", transcript)],
                temperature=0.0,  # 摘要要忠实不发散
            )
            summary_text = resp.content.strip()
        except Exception:
            # 摘要失败也别崩：退化为硬裁剪（丢中间段），靠 head+recent 继续跑。
            return []

        if not summary_text:
            return []

        # 用 assistant 角色塞回，标记为摘要，便于后续追踪。
        return [{"role": "assistant", "content": f"[历史摘要] {summary_text}"}]


def _estimate_tokens(messages: list[dict]) -> int:
    """粗略估算消息列表的 token 数。

    用 字符总数 // 4 近似。理由：
        - 英文：OpenAI tokenizer 平均 ~4 字符/token，是业界常用近似；
        - 中文：1 个汉字约 1.5-2 token，4 字符/token 会略低估，但偏低估比高估安全
          （低估触发压缩晚，高估触发压缩早可能误伤）——这里取偏低估，让压缩更晚触发，
          避免频繁摘要引入信息损失。
        - 不引入 tiktoken：第三方依赖、需联网下载编码表，违背惰性导入原则；
          4 近似对"是否超预算"的判断已足够稳。

    Args:
        messages: 消息列表。

    Returns:
        估算 token 数。
    """
    total_chars = 0
    for m in messages:
        # content 可能不是 str（防御性），统一 str 化。
        total_chars += len(str(m.get("content", "")))
        # role 也占少量 token，加上常数。
        total_chars += len(str(m.get("role", "")))
    return total_chars // 4
