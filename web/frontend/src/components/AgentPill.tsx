import { ChevronDown } from "lucide-react";

export interface AgentPillProps {
  currentAgent: { id: string; display_name: string; identity_color: string };
  open: boolean;
  onClick: () => void;
}

/** 智能体胶囊：composer 卡片内左上角，显示身份色首字 + 名称 + 展开箭头。 */
export default function AgentPill({ currentAgent, open, onClick }: AgentPillProps) {
  const initial = currentAgent.display_name ? currentAgent.display_name.charAt(0) : "?";
  return (
    <button
      type="button"
      className={`agent-pill ${open ? "open" : ""}`}
      aria-haspopup="listbox"
      aria-expanded={open}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      style={{ ["--identity-color" as string]: currentAgent.identity_color }}
    >
      <span className="ps">{initial}</span>
      {currentAgent.display_name}
      <ChevronDown className="chev" />
    </button>
  );
}