import { BookOpen, Layers, Palette } from "lucide-react";
import type { OutlineData } from "../api";

export interface OutlineCardProps {
  outline: OutlineData;
  /** 快捷选项（含"确认大纲"等），点击 = 填入输入框 */
  chips?: string[];
  onChipClick?: (text: string) => void;
}

/** theme id → 中文名（未知主题原样显示） */
const THEME_NAMES: Record<string, string> = {
  default: "暖橙",
  qinglan: "青蓝",
  moqing: "青绿",
  molv: "墨绿",
};

/** 判断 chip 语义：确认类为主按钮，主题类带调色板图标 */
function chipKind(chip: string): "confirm" | "theme" | "plain" {
  if (chip.includes("确认") || chip.includes("开始生成")) return "confirm";
  if (chip.includes("主题")) return "theme";
  return "plain";
}

/** 课件大纲卡片：嵌入消息流，头部元信息 + 按课时分组的页列表 + 底部确认 chips。 */
export default function OutlineCard({ outline, chips, onChipClick }: OutlineCardProps) {
  const meta = outline.meta ?? {};
  const pages = Array.isArray(outline.pages) ? outline.pages : [];
  const themeName = meta.theme ? THEME_NAMES[meta.theme] ?? meta.theme : "暖橙";

  // 按 period 分组（保持出现顺序；缺 period 归入第 1 课时）
  const groups: { period: number; pages: typeof pages }[] = [];
  for (const p of pages) {
    const period = typeof p.period === "number" ? p.period : 1;
    const hit = groups.find((g) => g.period === period);
    if (hit) hit.pages.push(p);
    else groups.push({ period, pages: [p] });
  }

  return (
    <div className="outline-card">
      <div className="oc-head">
        <span className="oc-icon"><BookOpen size={14} /></span>
        <b className="oc-title">{meta.title || "课件大纲"}</b>
        <span className="grow" />
        {meta.grade != null && <span className="oc-badge">第 {meta.grade} 年级</span>}
        {meta.lessonType && <span className="oc-badge">{meta.lessonType}</span>}
        {typeof meta.periods === "number" && meta.periods > 0 && (
          <span className="oc-badge"><Layers size={10} style={{ verticalAlign: "-1px", marginRight: 3 }} />{meta.periods} 课时</span>
        )}
        <span className="oc-badge theme"><Palette size={10} style={{ verticalAlign: "-1px", marginRight: 3 }} />{themeName}</span>
      </div>
      {meta.textbook && <div className="oc-sub">{meta.textbook}</div>}

      <div className="oc-pages">
        {groups.length === 0 ? (
          <div className="oc-empty">（暂无页面）</div>
        ) : (
          groups.map((g) => (
            <div key={g.period} className="oc-group">
              {groups.length > 1 && (
                <div className="oc-group-t">第 {g.period} 课时</div>
              )}
              {g.pages.map((p, i) => (
                <div key={p.id ?? `${g.period}-${i}`} className="oc-row">
                  <span className="oc-no">{pages.indexOf(p) + 1}</span>
                  {p.kind && <span className="oc-kind">{p.kind}</span>}
                  <div className="oc-row-main">
                    <b>{p.title || "（未命名页）"}</b>
                    {p.points && <span className="oc-points">{p.points}</span>}
                  </div>
                </div>
              ))}
            </div>
          ))
        )}
      </div>

      {Array.isArray(chips) && chips.length > 0 && (
        <div className="oc-chips">
          {chips.map((c, i) => {
            const kind = chipKind(c);
            return (
              <button
                key={i}
                type="button"
                className={`oc-chip ${kind}`}
                onClick={() => onChipClick?.(c)}
              >
                {kind === "theme" && <Palette size={12} style={{ verticalAlign: "-2px", marginRight: 4 }} />}
                {c}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
