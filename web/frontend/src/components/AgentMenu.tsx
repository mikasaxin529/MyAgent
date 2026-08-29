import type { AgentManifest } from "../api";

export interface AgentMenuProps {
  agents: AgentManifest[];
  currentId: string;
  onSelect: (agentId: string) => void;
  open: boolean;
  onClose: () => void;
}

/** 智能体选择下拉：GET /api/agents 填充，选中项打勾。 */
export default function AgentMenu({ agents, currentId, onSelect, open, onClose }: AgentMenuProps) {
  return (
    <div className={`menu ${open ? "show" : ""}`} role="listbox" aria-label="选择智能体">
      <div className="mhead">选择智能体 · 各自独立会话</div>
      {agents.length === 0 && (
        <div className="opt" role="presentation">
          <div className="om" style={{ color: "var(--text3)" }}>加载智能体失败，请刷新重试</div>
        </div>
      )}
      {agents.map((a) => (
        <button
          key={a.id}
          type="button"
          className="opt"
          role="option"
          aria-selected={a.id === currentId}
          onClick={() => {
            onSelect(a.id);
            onClose();
          }}
        >
          <span
            className="tile2"
            style={{ background: a.identity_color }}
          >
            {a.display_name ? a.display_name.charAt(0) : "?"}
          </span>
          <span className="om">
            <b>{a.display_name}</b>
            <span>{a.description}</span>
          </span>
          {a.id === currentId && <span className="tick">✓</span>}
        </button>
      ))}
      <div
        role="presentation"
        style={{
          padding: "9px 10px",
          borderTop: "1px solid var(--line)",
          marginTop: 4,
          color: "var(--text3)",
          fontSize: 11.5,
          lineHeight: 1.5,
        }}
      >
        新智能体放入 src/devpilot/agenthub/ 目录后自动发现
      </div>
    </div>
  );
}