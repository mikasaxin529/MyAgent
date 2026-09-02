import { BookOpen, Layers, Palette } from "lucide-react";
import type { OutlineData, ThemeOption } from "../api";

export interface OutlineCardProps {
  outline: OutlineData;
  /** 快捷选项（含"确认大纲"等），点击 = 填入输入框 */
  chips?: string[];
  onChipClick?: (text: string) => void;
}

/** 主题徽章显示名：优先帧内 options.themes（后端注册表派生，M1 即插即用）；
 * 未知主题（历史会话/options 缺失）回退内置四主题映射，再不济显示原名。 */
const FALLBACK_THEME_NAMES: Record<string, string> = {
  default: "暖橙",
  "fresh-blue": "青蓝",
  "warm-green": "墨绿",
  "mint-green": "青绿",
};

function themeDisplayName(themes: ThemeOption[] | undefined, name?: string): string {
  if (!name) return FALLBACK_THEME_NAMES["default"];
  const hit = themes?.find((t) => t.name === name);
  return hit?.display ?? FALLBACK_THEME_NAMES[name] ?? name;
}

/** 判断 chip 语义：确认类为主按钮，主题类带调色板图标 */
function chipKind(chip: string): "confirm" | "theme" | "plain" {
  if (chip.includes("确认") || chip.includes("开始生成")) return "confirm";
  if (chip.includes("主题")) return "theme";
  return "plain";
}

/** 课件大纲卡片：嵌入消息流，头部元信息 + 按课时分组的页列表 + 底部确认 chips。
 *  主题切换：options.themes 非空时渲染完整选择器（色卡+名称），点击即发
 *  "换<display>主题"；只有 chips 时退化为快捷按钮（旧会话兼容）。 */
export default function OutlineCard({ outline, chips, onChipClick }: OutlineCardProps) {
  const meta = outline.meta ?? {};
  const pages = Array.isArray(outline.pages) ? outline.pages : [];
  const themeOptions = outline.options?.themes ?? [];
  const themeName = themeDisplayName(themeOptions, meta.theme);

  // 按 period 分组（保持出现顺序；缺 period 归入第 1 课时）
  const groups: { period: number; pages: typeof pages }[] = [];
  for (const p of pages) {
    const period = typeof p.period === "number" ? p.period : 1;
    const hit = groups.find((g) => g.period === period);
    if (hit) hit.pages.push(p);
    else groups.push({ period, pages: [p] });
  }

  // 主题 chips（"换青蓝主题"）在有完整选择器时隐藏——选择器已覆盖其功能
  const visibleChips = themeOptions.length > 0
    ? (chips ?? []).filter((c) => !chipKind(c).includes("theme"))
    : chips;

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

      {themeOptions.length > 0 && (
        <div className="oc-themes" role="group" aria-label="切换主题">
          {themeOptions.map((t) => {
            const active = t.name === meta.theme;
            return (
              <button
                key={t.name}
                type="button"
                className={`oc-theme-btn${active ? " active" : ""}`}
                onClick={() => !active && onChipClick?.(`换${t.display}主题`)}
                title={t.tags?.length ? `${t.display} · ${t.tags.join("/")}` : t.display}
                aria-pressed={active}
              >
                {t.swatch?.length ? (
                  <span className="oc-swatch">
                    {t.swatch.slice(0, 3).map((c, i) => (
                      <i key={i} style={{ background: `#${c}` }} />
                    ))}
                  </span>
                ) : (
                  <Palette size={11} />
                )}
                <span>{t.display}</span>
              </button>
            );
          })}
        </div>
      )}

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

      {Array.isArray(visibleChips) && visibleChips.length > 0 && (
        <div className="oc-chips">
          {visibleChips.map((c, i) => {
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
