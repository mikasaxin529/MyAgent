import { useRef, useEffect, useState, forwardRef, useImperativeHandle } from "react";
import { ArrowUp } from "lucide-react";
import AgentPill from "./AgentPill";
import AgentMenu from "./AgentMenu";
import type { AgentManifest } from "../api";

export interface ComposerHandle {
  /** 外部填充输入框（chips 快捷选项点击等） */
  fill: (text: string) => void;
}

export interface ComposerProps {
  agentId: string;
  placeholder: string;
  loading: boolean;
  onSend: (text: string) => void;
  agents: AgentManifest[];
  currentAgent: { id: string; display_name: string; identity_color: string };
  onAgentSelect: (agentId: string) => void;
}

/** 卡片式输入栏 + 智能体胶囊下拉。 */
const Composer = forwardRef<ComposerHandle, ComposerProps>(function Composer(
  { placeholder, loading, onSend, agents, currentAgent, onAgentSelect },
  ref,
) {
  const [text, setText] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const composerRef = useRef<HTMLDivElement>(null);

  useImperativeHandle(ref, () => ({
    fill(chipText: string) {
      setText(chipText);
      textareaRef.current?.focus();
    },
  }));

  // Auto-resize textarea
  useEffect(() => {
    const ta = textareaRef.current;
    if (ta) {
      ta.style.height = "auto";
      ta.style.height = Math.min(ta.scrollHeight, 160) + "px";
    }
  }, [text]);

  // Close menu on outside click
  useEffect(() => {
    if (!menuOpen) return;
    function handleClick(e: MouseEvent) {
      if (composerRef.current && !composerRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    }
    document.addEventListener("click", handleClick);
    return () => document.removeEventListener("click", handleClick);
  }, [menuOpen]);

  function handleSubmit() {
    const trimmed = text.trim();
    if (!trimmed || loading) return;
    onSend(trimmed);
    setText("");
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  }

  return (
    <div className="composer-wrap">
      <div className="composer" ref={composerRef}>
        <AgentMenu
          agents={agents}
          currentId={currentAgent.id}
          onSelect={onAgentSelect}
          open={menuOpen}
          onClose={() => setMenuOpen(false)}
        />
        <div className="composer-card">
          <AgentPill
            currentAgent={currentAgent}
            open={menuOpen}
            onClick={() => setMenuOpen((v) => !v)}
          />
          <textarea
            ref={textareaRef}
            rows={2}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={placeholder || "输入需求，如「帮我总结7月最新AI资讯」"}
            disabled={loading}
          />
          <div className="composer-bar">
            <span className="hint">Enter 发送 · Shift+Enter 换行 · 会话历史仅此智能体可见</span>
            <button
              type="button"
              className="send"
              aria-label="发送"
              disabled={loading || !text.trim()}
              onClick={handleSubmit}
            >
              {loading ? (
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ width: 15, height: 15, animation: "spin 1s linear infinite" }}>
                  <circle cx="12" cy="12" r="10" strokeDasharray="31.4 31.4" />
                </svg>
              ) : (
                <ArrowUp />
              )}
            </button>
          </div>
        </div>
      </div>
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
});

export default Composer;