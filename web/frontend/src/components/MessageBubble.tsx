import { useState } from "react";
import { Check, Copy, RotateCw, Bot } from "lucide-react";
import type { Message } from "../api";
import { downloadFile } from "../api";
import MarkdownRenderer from "./MarkdownRenderer";
import ThoughtBlock from "./ThoughtBlock";
import FileCard from "./FileCard";

export interface MessageBubbleProps {
  message: Message;
  identityColor?: string;
  /** 是否是最后一条消息（决定挂不挂"重新生成"） */
  isLast?: boolean;
  onRegenerate?: () => void;
  onChipClick?: (text: string) => void;
}

/** 单条消息（千问式）：助手无头像无署名全宽平铺，用户浅灰气泡右对齐。 */
export default function MessageBubble({ message, identityColor, isLast, onRegenerate, onChipClick }: MessageBubbleProps) {
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

  return (
    <div className="msg ai">
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
              <FileCard key={i} file={f} onDownload={downloadFile} />
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
      {/* 千问式操作条：正文下方左对齐灰色图标组，完成后出现 */}
      {message.content && message.done && (
        <div className="msg-actions">
          <button onClick={copyContent} className="ma-btn" title={copied ? "已复制" : "复制"}>
            {copied ? <Check size={16} /> : <Copy size={16} />}
          </button>
          {isLast && onRegenerate && (
            <button onClick={onRegenerate} className="ma-btn" title="重新生成">
              <RotateCw size={16} />
            </button>
          )}
        </div>
      )}
    </div>
  );
}
