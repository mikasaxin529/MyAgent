import { Clapperboard, Film, Users } from "lucide-react";
import type { StorySynopsisData } from "../api";

export interface SynopsisCardProps {
  synopsis: StorySynopsisData;
  /** 快捷选项（含"确认梗概"等），点击 = 填入输入框 */
  chips?: string[];
  onChipClick?: (text: string) => void;
}

/** 判断 chip 语义：确认类为主按钮（与 OutlineCard.chipKind 同规则） */
function chipKind(chip: string): "confirm" | "plain" {
  return chip.includes("确认") ? "confirm" : "plain";
}

/** 故事梗概卡片（story_synopsis 帧）：头部片名+主题徽章 → logline →
 *  三幕结构 → 梗概正文 → 角色速写 → 底部确认 chips。 */
export default function SynopsisCard({ synopsis, chips, onChipClick }: SynopsisCardProps) {
  const acts = Array.isArray(synopsis.acts) ? synopsis.acts : [];
  const briefs = Array.isArray(synopsis.characters_brief) ? synopsis.characters_brief : [];
  const themes = Array.isArray(synopsis.themes) ? synopsis.themes : [];

  return (
    <div className="outline-card story-card">
      <div className="oc-head">
        <span className="oc-icon"><Clapperboard size={14} /></span>
        <b className="oc-title">{synopsis.title || "故事梗概"}</b>
        <span className="grow" />
        {themes.map((t) => (
          <span key={t} className="oc-badge theme">{t}</span>
        ))}
        {typeof synopsis.scene_count === "number" && synopsis.scene_count > 0 && (
          <span className="oc-badge"><Film size={10} style={{ verticalAlign: "-1px", marginRight: 3 }} />约 {synopsis.scene_count} 场</span>
        )}
      </div>
      {synopsis.logline && (
        <div className="sc-logline">{synopsis.logline}</div>
      )}

      {acts.length > 0 && (
        <div className="sc-acts">
          {acts.map((a, i) => (
            <div key={i} className="sc-act">
              <b className="sc-act-t">{a.act || `第${i + 1}幕`}</b>
              <span className="sc-act-s">{a.summary}</span>
            </div>
          ))}
        </div>
      )}

      {synopsis.synopsis && (
        <div className="sc-synopsis">{synopsis.synopsis}</div>
      )}

      {briefs.length > 0 && (
        <div className="sc-chars">
          <div className="sc-chars-t"><Users size={11} style={{ verticalAlign: "-1px", marginRight: 4 }} />角色速写</div>
          {briefs.map((c, i) => (
            <div key={i} className="sc-char">
              <b>{c.name}</b>
              <span>{c.desc}</span>
            </div>
          ))}
        </div>
      )}

      {Array.isArray(chips) && chips.length > 0 && (
        <div className="oc-chips">
          {chips.map((c, i) => (
            <button
              key={i}
              type="button"
              className={`oc-chip ${chipKind(c)}`}
              onClick={() => onChipClick?.(c)}
            >
              {c}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
