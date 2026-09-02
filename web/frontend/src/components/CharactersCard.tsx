import { UserRound } from "lucide-react";
import type { StoryCharactersData } from "../api";

export interface CharactersCardProps {
  characters: StoryCharactersData;
  /** 快捷选项（含"确认角色"等），点击 = 填入输入框 */
  chips?: string[];
  onChipClick?: (text: string) => void;
}

function chipKind(chip: string): "confirm" | "plain" {
  return chip.includes("确认") ? "confirm" : "plain";
}

/** 角色卡卡片（story_characters 帧）：角色名+定位徽章 → 立绘图（有则显示）→
 *  视觉锚点 description（全片形象以这段为准）。
 *  双层锚点说明：description 是文字锚，portrait 是标准立绘参照。 */
export default function CharactersCard({ characters, chips, onChipClick }: CharactersCardProps) {
  const chars = Array.isArray(characters.characters) ? characters.characters : [];

  return (
    <div className="outline-card story-card">
      <div className="oc-head">
        <span className="oc-icon"><UserRound size={14} /></span>
        <b className="oc-title">角色设定</b>
        <span className="grow" />
        <span className="oc-badge">{chars.length} 个角色</span>
      </div>

      {chars.length === 0 ? (
        <div className="oc-empty">（暂无角色）</div>
      ) : (
        <div className="sc-char-grid">
          {chars.map((c, i) => (
            <div key={c.id ?? i} className="sc-charcard">
              {(c.portrait_url || c.portrait) && (
                <img
                  className="sc-portrait"
                  src={c.portrait_url ?? `/files/story/${c.portrait}`}
                  alt={c.name || "角色立绘"}
                  loading="lazy"
                />
              )}
              <div className="sc-charcard-body">
                <div className="sc-charcard-head">
                  <b>{c.name || "（未命名）"}</b>
                  {c.role && <span className="sc-role">{c.role}</span>}
                </div>
                <p className="sc-desc">{c.description}</p>
                {c.ref_prompt && (
                  <details className="sc-ref">
                    <summary>立绘提示词</summary>
                    <span>{c.ref_prompt}</span>
                  </details>
                )}
              </div>
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
