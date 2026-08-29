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
  if (message.role === "user") {
    return (
      <div className="msg user">
        <div className="bubble">{message.content}</div>
      </div>
    );
  }

  const hasThoughts = message.reasoning.length > 0 || message.steps.length > 0;
  const [copied, setCopied] = useState(false);
  function copyContent() {
    navigator.clipboard.writeText(message.content).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }

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
        {message.done && message.meta && (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 10 }}>
            <MetaCard label="流程" value={(message.meta.nodes_visited ?? []).join(" → ")} />
            <MetaCard label="Audit" value={`${message.meta.audit_total}`} />
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
      {message.content && (
        <button
          onClick={copyContent}
          className="flex items-center gap-1 px-2 py-1 rounded-md border text-[11px] transition"
          style={{
            marginTop: 8,
            cursor: "pointer",
            fontFamily: "inherit",
            color: "var(--text2)",
            background: "var(--bg)",
            borderColor: "var(--line)",
          }}
        >
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? "已复制" : "复制"}
        </button>
      )}
    </div>
  );
}

function MetaCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="px-3 py-2 rounded-md" style={{ background: "var(--sunken)", border: "1px solid var(--line)" }}>
      <div style={{ color: "var(--text3)", fontSize: 10, textTransform: "uppercase", letterSpacing: "0.05em" }}>{label}</div>
      <div style={{ color: "var(--text2)", fontSize: 13, marginTop: 2 }}>{value}</div>
    </div>
  );
}