import { FormEvent, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import {
  Bot,
  User,
  Copy,
  Check,
  Send,
  Brain,
  ChevronRight,
  Globe,
  Search,
  FileText,
  ListTree,
  type LucideIcon,
} from "lucide-react";
import { ChatSocket, type ChatFrame } from "../api";
import FlowGraph, { type NodeUpdate, type PlanStep } from "../components/FlowGraph";

// highlight.js 的 github-dark 主题样式（代码块语法高亮配色）
import "highlight.js/styles/github-dark.css";

/** 单条聊天消息。assistant 消息在流式过程中被逐帧追加 content/reasoning/steps。 */
interface Message {
  role: "user" | "assistant";
  content: string;
  reasoning: string; // 思考过程增量（reasoning 帧累积）
  steps: StepItem[]; // route / node / step 帧归集到此（文字时间线）
  done: boolean; // 是否已收到 done 终帧
  meta?: { nodes_visited: string[]; audit_total: number }; // done 帧的 meta
  error?: string; // error 帧的消息
}

/** assistant 消息上方"思考过程"中的步骤项。kind 区分来源帧。 */
interface StepItem {
  kind: "plan" | "node" | "step";
  text: string;
}

export default function ChatPage() {
  const [prompt, setPrompt] = useState("");
  const [loading, setLoading] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);

  // 图形流状态：plan 是动态步骤计划（收到 plan 帧后据此重建图），
  // nodeUpdates 累积 node 帧状态变化。
  const [plan, setPlan] = useState<PlanStep[] | null>(null);
  const [nodeUpdates, setNodeUpdates] = useState<NodeUpdate[]>([]);

  const socketRef = useRef<ChatSocket | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // 组件卸载时关闭 WS，避免泄漏
  useEffect(() => {
    return () => socketRef.current?.close();
  }, []);

  // 自动滚动：messages 一旦变化（每来一个 token）就把消息区滚到底部
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight });
  }, [messages]);

  function onSend(e: FormEvent) {
    e.preventDefault();
    const text = prompt.trim();
    if (!text || loading) return;

    // 1. 立即追加 user 消息；同时 push 一条 assistant 占位消息，后续帧追加到它
    const userMsg: Message = {
      role: "user",
      content: text,
      reasoning: "",
      steps: [],
      done: false,
    };
    const assistantMsg: Message = {
      role: "assistant",
      content: "",
      reasoning: "",
      steps: [],
      done: false,
    };
    setMessages((prev) => [...prev, userMsg, assistantMsg]);
    setPrompt("");
    setLoading(true);

    // 新对话开始：重置图形流（清空上次的计划与节点状态，等 planner 重新规划）。
    setPlan(null);
    setNodeUpdates([]);

    // 2. 建立新 WS。onOpen 时把 prompt 发出去。
    const sock = new ChatSocket({
      onOpen: () => sock.sendPrompt(text),
      onFrame: (frame) => handleFrame(frame),
      onClose: () => {
        // 连接关闭时若仍未 done，标记完成并清掉 loading
        setLoading(false);
        setMessages((prev) => patchLastAssistant(prev, (m) => ({ ...m, done: true })));
      },
      onError: () => {
        setLoading(false);
        setMessages((prev) =>
          patchLastAssistant(prev, (m) => ({ ...m, error: "连接异常", done: true })),
        );
      },
    });
    socketRef.current = sock;
    sock.connect();
  }

  // 帧分发：每帧都"追加到最后一条 assistant 消息"
  function handleFrame(frame: ChatFrame) {
    switch (frame.type) {
      case "token":
        setMessages((prev) =>
          patchLastAssistant(prev, (m) => ({ ...m, content: m.content + frame.delta })),
        );
        break;
      case "reasoning":
        setMessages((prev) =>
          patchLastAssistant(prev, (m) => ({ ...m, reasoning: m.reasoning + frame.delta })),
        );
        break;
      case "plan":
        // planner 生成的步骤计划：据此动态重建右侧节点图。
        setPlan(frame.steps);
        setMessages((prev) =>
          patchLastAssistant(prev, (m) => ({
            ...m,
            steps: [
              ...m.steps,
              {
                kind: "plan",
                text: `规划 ${frame.steps.length} 步：${frame.steps
                  .map((s, i) => `${i + 1}.${s.name}${s.needs_search ? "🌐" : ""}`)
                  .join(" → ")}`,
              },
            ],
          })),
        );
        break;
      case "node":
        setNodeUpdates((prev) => [
          ...prev,
          { node_id: frame.node_id, status: frame.status },
        ]);
        setMessages((prev) =>
          patchLastAssistant(prev, (m) => ({
            ...m,
            steps: [
              ...m.steps,
              { kind: "node", text: `${frame.node_id} → ${frame.status}` },
            ],
          })),
        );
        break;
      case "step":
        setMessages((prev) =>
          patchLastAssistant(prev, (m) => ({
            ...m,
            steps: [...m.steps, { kind: "step", text: JSON.stringify(frame.step) }],
          })),
        );
        break;
      case "blackboard":
        // dev 分支黑板快照：作为一条 step 文字记录，本页不渲染图形
        setMessages((prev) =>
          patchLastAssistant(prev, (m) => ({
            ...m,
            steps: [...m.steps, { kind: "step", text: `黑板快照: ${JSON.stringify(frame.data)}` }],
          })),
        );
        break;
      case "done":
        setMessages((prev) =>
          patchLastAssistant(prev, (m) => ({
            // done 帧可能带最终 answer，若 token 流为空则用它作正文
            ...m,
            content: m.content || frame.answer,
            done: true,
            meta: frame.meta,
          })),
        );
        setLoading(false);
        socketRef.current?.close();
        break;
      case "error":
        setMessages((prev) =>
          patchLastAssistant(prev, (m) => ({ ...m, error: frame.message, done: true })),
        );
        setLoading(false);
        socketRef.current?.close();
        break;
    }
  }

  return (
    <div className="h-full flex">
      {/* 左侧：聊天区 */}
      <div className="flex-1 flex flex-col min-w-0">
        <header className="px-6 py-4 border-b border-slate-800 flex items-center gap-2">
          <Bot size={18} className="text-emerald-400" />
          <div>
            <h1 className="text-lg font-semibold">Chat</h1>
            <p className="text-xs text-slate-500">动态编排 · 流式输出 · 思考过程</p>
          </div>
        </header>

        {/* 消息区：flex-1 占满，overflow-auto，自动滚到底 */}
        <div ref={scrollRef} className="flex-1 overflow-auto px-6 py-6 space-y-5">
          {messages.length === 0 && (
            <div className="h-full flex flex-col items-center justify-center gap-3 text-slate-600">
              <Bot size={32} className="text-slate-700" />
              <span className="text-sm">输入需求，模型将动态规划步骤并执行</span>
            </div>
          )}
          {messages.map((m, i) => (
            <MessageBubble key={i} message={m} />
          ))}
        </div>

        {/* 底部输入栏 */}
        <form onSubmit={onSend} className="px-6 py-4 border-t border-slate-800 flex gap-3 items-center">
          <input
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="输入需求，如「帮我总结7月最新AI资讯」(Enter 发送)"
            className="flex-1 px-4 py-2.5 rounded-xl bg-slate-900 border border-slate-700 text-sm focus:outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
          />
          <button
            type="submit"
            disabled={loading || !prompt.trim()}
            className="flex items-center gap-1.5 px-5 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 disabled:bg-slate-800 disabled:text-slate-600 text-white text-sm font-medium transition"
          >
            {loading ? (
              <>
                <Bot size={15} className="animate-pulse" />
                生成中…
              </>
            ) : (
              <>
                <Send size={15} />
                发送
              </>
            )}
          </button>
        </form>
      </div>

      {/* 右侧：Dify 式动态图形流，据 planner 的步骤计划生成节点图 */}
      <div className="w-[420px] border-l border-slate-800 flex flex-col">
        <div className="px-4 py-2 text-xs uppercase tracking-wider text-slate-500 border-b border-slate-800">
          编排图 · {plan ? `${plan.length} 步计划` : "等待规划"}
        </div>
        <div className="flex-1">
          <FlowGraph plan={plan} nodeUpdates={nodeUpdates} />
        </div>
      </div>
    </div>
  );
}

/** 对最后一条 assistant 消息做不可变更新；非 assistant 末条则原样返回（防御）。 */
function patchLastAssistant(
  prev: Message[],
  fn: (m: Message) => Message,
): Message[] {
  if (prev.length === 0) return prev;
  const last = prev[prev.length - 1];
  if (last.role !== "assistant") return prev;
  return [...prev.slice(0, -1), fn(last)];
}

/** 单条消息气泡：ChatGPT 式——左侧头像 + 右侧内容（思考过程 + markdown 正文 + 复制按钮）。 */
function MessageBubble({ message }: { message: Message }) {
  const isUser = message.role === "user";
  const hasThoughts = message.reasoning.length > 0 || message.steps.length > 0;
  const [copied, setCopied] = useState(false);

  function copyContent() {
    navigator.clipboard.writeText(message.content).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }

  const Avatar = isUser ? User : Bot;

  return (
    <div className={`flex gap-3 ${isUser ? "flex-row-reverse" : "flex-row"}`}>
      {/* 头像：圆形，区分 user/assistant 配色 */}
      <div
        className={`shrink-0 h-8 w-8 rounded-full flex items-center justify-center ${
          isUser ? "bg-indigo-600" : "bg-gradient-to-br from-emerald-500 to-teal-600"
        }`}
      >
        <Avatar size={16} className="text-white" />
      </div>

      <div className={`flex flex-col gap-2 min-w-0 ${isUser ? "items-end" : "items-start"} ${isUser ? "max-w-[75%]" : "flex-1 max-w-[88%]"}`}>
        {!isUser && hasThoughts && <ThoughtBlock message={message} />}

        <div
          className={
            isUser
              ? "px-4 py-2.5 rounded-2xl rounded-tr-sm bg-indigo-600 text-white text-sm whitespace-pre-wrap"
              : "group relative px-4 py-3 rounded-2xl rounded-tl-sm bg-slate-900 border border-slate-800 text-sm leading-relaxed"
          }
        >
          {isUser ? (
            message.content
          ) : message.content ? (
            <MarkdownRenderer content={message.content} />
          ) : message.error ? (
            <span className="text-rose-300">{message.error}</span>
          ) : (
            <span className="inline-flex items-center gap-1 text-slate-500 italic">
              <Bot size={13} className="animate-pulse" /> 生成中…
            </span>
          )}

          {/* assistant 消息 hover 显示复制按钮 */}
          {!isUser && message.content && (
            <button
              onClick={copyContent}
              className="absolute -bottom-3 right-2 opacity-0 group-hover:opacity-100 transition flex items-center gap-1 px-2 py-1 rounded-md bg-slate-800 border border-slate-700 text-[11px] text-slate-300 hover:bg-slate-700"
              title="复制"
            >
              {copied ? <Check size={12} /> : <Copy size={12} />}
              {copied ? "已复制" : "复制"}
            </button>
          )}
        </div>

        {!isUser && message.done && message.meta && (
          <div className="flex flex-wrap gap-2">
            <MetaCard label="流程" value={(message.meta.nodes_visited ?? []).join(" → ")} />
            <MetaCard label="Audit" value={`${message.meta.audit_total}`} />
          </div>
        )}
      </div>
    </div>
  );
}

/** ChatGPT 式 markdown 渲染：精致排版 + 代码块（语言标签/复制/语法高亮）。 */
function MarkdownRenderer({ content }: { content: string }) {
  return (
    <div className="prose-invert max-w-none text-slate-100">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          // 区分行内 code 与代码块：react-markdown v9 里 code 节点带 className=language-xxx 的是块。
          code({ className, children }) {
            const isBlock = /language-/.test(className || "");
            if (!isBlock) {
              // 行内代码：小标签样式
              return (
                <code className="bg-slate-700/60 px-1.5 py-0.5 rounded text-emerald-300 font-mono text-[0.85em]">
                  {children}
                </code>
              );
            }
            // 代码块：交给 pre 处理（下方 pre component 加语言标签+复制）。
            const match = /language-(\w+)/.exec(className || "");
            const lang = match ? match[1] : "code";
            return <CodeBlock lang={lang}>{String(children).replace(/\n$/, "")}</CodeBlock>;
          },
          // pre：代码块外层由 code 返回的 CodeBlock 接管，这里直接渲染 children 避免双层 pre。
          pre({ children }) {
            return <>{children}</>;
          },
          a({ children, href }) {
            return (
              <a
                href={href}
                target="_blank"
                rel="noreferrer"
                className="text-indigo-400 hover:text-indigo-300 underline decoration-indigo-500/40 underline-offset-2"
              >
                {children}
              </a>
            );
          },
          ul({ children }) {
            return <ul className="list-disc list-outside ml-5 my-2 space-y-1">{children}</ul>;
          },
          ol({ children }) {
            return <ol className="list-decimal list-outside ml-5 my-2 space-y-1">{children}</ol>;
          },
          li({ children }) {
            return <li className="text-sm">{children}</li>;
          },
          blockquote({ children }) {
            return (
              <blockquote className="border-l-2 border-indigo-500/50 pl-3 my-2 text-slate-400 italic">
                {children}
              </blockquote>
            );
          },
          h1({ children }) {
            return <h1 className="text-lg font-semibold mt-4 mb-2 text-slate-50">{children}</h1>;
          },
          h2({ children }) {
            return <h2 className="text-base font-semibold mt-3 mb-2 text-slate-50">{children}</h2>;
          },
          h3({ children }) {
            return <h3 className="text-sm font-semibold mt-3 mb-1 text-slate-100">{children}</h3>;
          },
          p({ children }) {
            return <p className="my-2 first:mt-0 last:mb-0">{children}</p>;
          },
          table({ children }) {
            return (
              <div className="overflow-x-auto my-3 rounded-lg border border-slate-800">
                <table className="min-w-full text-xs">{children}</table>
              </div>
            );
          },
          th({ children }) {
            return <th className="px-3 py-2 bg-slate-800 text-left font-semibold text-slate-200">{children}</th>;
          },
          td({ children }) {
            return <td className="px-3 py-2 border-t border-slate-800 text-slate-300">{children}</td>;
          },
          hr() {
            return <hr className="my-4 border-slate-800" />;
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

/** ChatGPT 式代码块：顶部栏（语言标签 + 复制按钮）+ 语法高亮正文。 */
function CodeBlock({ lang, children }: { lang: string; children: string }) {
  const [copied, setCopied] = useState(false);
  function copy() {
    navigator.clipboard.writeText(children).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }
  return (
    <div className="my-3 rounded-lg overflow-hidden border border-slate-800 bg-[#0d1117]">
      {/* 顶部栏：语言标签 + 复制按钮 */}
      <div className="flex items-center justify-between px-3 py-1.5 bg-slate-800/60 border-b border-slate-800">
        <span className="text-[11px] text-slate-400 font-mono">{lang}</span>
        <button
          onClick={copy}
          className="flex items-center gap-1 text-[11px] text-slate-400 hover:text-slate-200 transition"
        >
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      {/* 代码正文：等宽 + 横向滚动 + hljs 高亮（rehype-highlight 生成 class） */}
      <pre className="overflow-x-auto px-4 py-3 text-[13px] leading-relaxed">
        <code className={`hljs language-${lang}`}>{children}</code>
      </pre>
    </div>
  );
}

/** 思考过程可折叠区：图标 + reasoning 文本 + steps 时间线（带步骤类型图标）。 */
function ThoughtBlock({ message }: { message: Message }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="w-full">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 text-xs text-slate-400 hover:text-slate-200 transition"
      >
        <ChevronRight size={13} className={`transition-transform ${open ? "rotate-90" : ""}`} />
        <Brain size={13} />
        思考过程
        <span className="text-slate-600">({message.steps.length} 步)</span>
      </button>
      {open && (
        <div className="mt-1.5 px-3 py-2 rounded-lg bg-slate-900/60 border border-slate-800 space-y-2">
          {message.reasoning && (
            <p className="whitespace-pre-wrap text-slate-400 italic text-xs leading-relaxed">
              {message.reasoning}
            </p>
          )}
          {message.steps.length > 0 && (
            <ul className="space-y-1">
              {message.steps.map((s, i) => (
                <li key={i} className="flex items-start gap-1.5 text-[11px] text-slate-400 font-mono">
                  <StepIcon kind={s.kind} />
                  <span>{s.text}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

/** 步骤类型图标：plan/node/step 用不同 lucide 图标区分。 */
function StepIcon({ kind }: { kind: string }) {
  const Icon: LucideIcon =
    kind === "plan" ? ListTree : kind === "node" ? FileText : kind === "step" ? Search : Globe;
  return <Icon size={12} className="shrink-0 mt-0.5 text-slate-500" />;
}

function MetaCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="px-3 py-2 rounded-md bg-slate-900 border border-slate-800">
      <div className="text-[10px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className="text-sm text-slate-200 mt-0.5">{value}</div>
    </div>
  );
}
