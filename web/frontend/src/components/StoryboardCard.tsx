import { Clapperboard, MessageSquareQuote, Volume2 } from "lucide-react";
import type { StoryStoryboardData } from "../api";

export interface StoryboardCardProps {
  storyboard: StoryStoryboardData;
  /** 快捷选项（含"确认分镜"等），点击 = 填入输入框 */
  chips?: string[];
  /** 总镜数（帧冗余字段；历史帧缺失时回退自算） */
  nShots?: number;
  onChipClick?: (text: string) => void;
}

function chipKind(chip: string): "confirm" | "plain" {
  return chip.includes("确认") ? "confirm" : "plain";
}

/** 分镜脚本卡片（story_storyboard 帧）：按场分组 → 镜头行
 *  （镜号/景别/运镜/画面描述/台词/音效）。 */
export default function StoryboardCard({ storyboard, chips, nShots, onChipClick }: StoryboardCardProps) {
  const scenes = Array.isArray(storyboard.scenes) ? storyboard.scenes : [];
  const totalShots = typeof nShots === "number"
    ? nShots
    : scenes.reduce((n, sc) => n + (sc.shots?.length ?? 0), 0);

  return (
    <div className="outline-card story-card">
      <div className="oc-head">
        <span className="oc-icon"><Clapperboard size={14} /></span>
        <b className="oc-title">分镜脚本</b>
        <span className="grow" />
        <span className="oc-badge">{scenes.length} 场</span>
        <span className="oc-badge">{totalShots} 镜</span>
      </div>

      <div className="sb-scenes">
        {scenes.length === 0 ? (
          <div className="oc-empty">（暂无分镜）</div>
        ) : (
          scenes.map((sc, si) => (
            <section key={sc.scene_no ?? si} className="sb-scene">
              <header className="sb-scene-head">
                <b>第 {sc.scene_no ?? si + 1} 场</b>
                <span className="sb-slug">{sc.slug}</span>
                {sc.synopsis && <span className="sb-scene-syn">{sc.synopsis}</span>}
              </header>
              {(sc.shots ?? []).map((sh, i) => (
                <div key={sh.id ?? i} className="sb-shot">
                  <div className="sb-shot-meta">
                    <span className="sb-shot-id">{sh.id}</span>
                    {sh.shot_size && <span className="sb-tag">{sh.shot_size}</span>}
                    {sh.camera && <span className="sb-tag">{sh.camera}</span>}
                  </div>
                  {sh.image_prompt && (
                    <div className="sb-prompt">{sh.image_prompt}</div>
                  )}
                  {sh.dialogue && (
                    <div className="sb-dialogue">
                      <MessageSquareQuote size={11} />
                      <span>{sh.dialogue}</span>
                    </div>
                  )}
                  {sh.sfx && (
                    <div className="sb-sfx"><Volume2 size={11} />{sh.sfx}</div>
                  )}
                </div>
              ))}
            </section>
          ))
        )}
      </div>

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
