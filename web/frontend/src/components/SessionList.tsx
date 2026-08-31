import { MessageSquare, Trash2 } from "lucide-react";
import type { SessionGroup } from "../api";

export interface SessionListProps {
  sessions: SessionGroup[];
  activeAgentId: string;
  activeSessionId: string;
  onSelectSession: (agentId: string, sessionId: string) => void;
  onDeleteSession: (agentId: string, sessionId: string) => void;
  onNewSession: (agentId: string) => void;
  onClose: () => void;
}

/** 会话列表：按 agent 分组，显示在 rail 侧；悬停条目出现删除按钮。 */
export default function SessionList({
  sessions,
  activeAgentId,
  activeSessionId,
  onSelectSession,
  onDeleteSession,
  onNewSession,
  onClose,
}: SessionListProps) {
  return (
    <>
      <div className="session-list-overlay" onClick={onClose} style={{
        position: "fixed", inset: 0, zIndex: 29, background: "rgba(0,0,0,0.2)"
      }} />
      <div className="session-list">
        <div className="session-list-head">
          <span>会话历史</span>
          <button
            onClick={onClose}
            style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text3)", fontSize: 16 }}
            aria-label="关闭"
          >
            ×
          </button>
        </div>
        <div className="session-list-body">
          {sessions.length === 0 && (
            <div style={{ textAlign: "center", color: "var(--text3)", fontSize: 12.5, padding: "24px 0" }}>
              暂无会话
            </div>
          )}
          {sessions.map((group) => (
            <div key={group.agentId} className="session-group">
              <div className="session-group-label">
                <span className="sg-dot" style={{ background: group.identityColor }} />
                {group.displayName}
              </div>
              {group.sessions.length === 0 && (
                <div className="session-item" onClick={() => onNewSession(group.agentId)}>
                  <span className="si-new">+ 新会话</span>
                </div>
              )}
              {group.sessions.map((sess) => (
                <div
                  key={sess.id}
                  className={`session-item ${group.agentId === activeAgentId && sess.id === activeSessionId ? "active" : ""}`}
                  onClick={() => onSelectSession(group.agentId, sess.id)}
                >
                  <MessageSquare size={14} style={{ flexShrink: 0, color: "var(--text3)" }} />
                  <span className="si-title">{sess.title}</span>
                  <span className="si-time">{new Date(sess.updatedAt * 1000).toLocaleDateString("zh-CN")}</span>
                  <button
                    className="si-del"
                    aria-label="删除会话"
                    title="删除会话"
                    onClick={(e) => {
                      e.stopPropagation();
                      if (window.confirm(`删除会话「${sess.title}」？该操作不可恢复。`)) {
                        onDeleteSession(group.agentId, sess.id);
                      }
                    }}
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
              ))}
              <div className="session-item" onClick={() => onNewSession(group.agentId)}>
                <span className="si-new">+ 新会话</span>
              </div>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}
