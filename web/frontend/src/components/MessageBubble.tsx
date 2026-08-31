import { useState } from "react";
import { Check, Copy, Bot } from "lucide-react";
import type { Message } from "../api";
import MarkdownRenderer from "./MarkdownRenderer";
import ThoughtBlock from "./ThoughtBlock";
import FileCard from "./FileCard";

export interface MessageBubbleProps {
  message: Message;
  identityColor?: string;
  agentName?: string;
  onChipClick?: (text: string) => void;
}

/** 单条消息：助手无气泡署名式全宽排版，用户气泡右对齐。 */
export default function MessageBubble({ message, identityColor, agentName, onChipClick }: MessageBubbleProps) {
  const [copied, setCopied] = useState(false);
  function copyContent() {
    navigator.clipboard.writeText(message.content).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }

  if (message.role === "user") {
    return (
      <div className="msg user">
        <div className="bubble">{message.content}</div>
      </div>
    );
  }

  const hasThoughts = message.reasoning.length > 0 || message.steps.length > 0;

  const timeStr = message.ts
    ? new Date(message.ts * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })
    : "";

  // ---- think-line: "思考 N 秒 · 末步骤名" from step timestamps ----
  const thinkLine = (() => {
    if (message.steps.length === 0) return null;
    const doneSteps = message.steps.filter((s) => s.status === "done");
    if (doneSteps.length === 0) return null;
    const firstTs = Math.min(...message.steps.map((s) => s.ts));
    const lastDone = doneSteps[doneSteps.length - 1];
    const totalSec = (lastDone.ts - firstTs).toFixed(1);
    return { totalSec, label: lastDone.label };
  })();

  return (
    <div className="msg ai">
      <div className="author">
        <span className="mini-seal" style={{ background: identityColor ?? "var(--seal)" }}>
          {agentName?.[0] ?? "AI"}
        </span>
        <b>{agentName ?? "智能体"}</b>
        <time>{timeStr}</time>
      </div>
      {thinkLine && (
        <div className="think-line">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="10" />
            <path d="M12 6v6l4 2" />
          </svg>
          思考 {thinkLine.totalSec}s · {thinkLine.label}
        </div>
      )}
      <div className="body">
        {hasThoughts && (
          <ThoughtBlock
            reasoning={message.reasoning}
            steps={message.steps}
            files={message.files}
            identityColor={identityColor}
          />
        )}
        {message.error ? (
          <p style={{ color: "var(--err)", margin: "6px 0" }}>
            {message.error}
          </p>
        ) : message.content ? (
          <MarkdownRenderer content={message.content} />
        ) : message.done ? (
          <p style={{ color: "var(--text3)", margin: 0 }}>（无输出）</p>
        ) : (
          <p style={{ color: "var(--text3)", margin: 0, display: "inline-flex", alignItems: "center", gap: 6 }}>
            <Bot size={13} className="animate-pulse" /> 生成中…
          </p>
        )}
        {message.files.length > 0 && (
          <div className="files">
            {message.files.map((f, i) => (
              <FileCard key={i} file={f} onDownload={(p) => window.open(p, "_blank")} />
            ))}
          </div>
        )}
        {Array.isArray(message.chips) && message.chips.length > 0 && (
          <div className="chips">
            {message.chips.map((c, i) => (
              <button
                key={i}
                type="button"
                className="chip"
                style={{ cursor: "pointer" }}
                onClick={() => onChipClick?.(c)}
              >
                {c}
              </button>
            ))}
          </div>
        )}
      </div>
      {/* ChatGPT 式右下角悬浮操作条：hover 消息时出现，元信息在左、复制在右 */}
      {message.content && (
        <div className="msg-actions">
          {message.done && message.meta && (
            <span className="ma-meta" title="节点执行路径与审计步数">
              {(message.meta.nodes_visited ?? []).join(" → ")}
              <i>·</i>
              Audit {message.meta.audit_total}
            </span>
          )}
          <span className="ma-spacer" />
          <button
            onClick={copyContent}
            className="ma-btn"
            title={copied ? "已复制" : "复制回答"}
          >
            {copied ? <Check size={14} /> : <Copy size={14} />}
          </button>
        </div>
      )}
    </div>
  );
}