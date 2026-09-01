import { Eye, EyeOff } from "lucide-react";
import type { VisualIssue, VisualReviewData } from "../api";

export interface VisualReviewCardProps {
  visual: VisualReviewData;
}

/** 视觉问题类型中文标签（与后端 issues[].type 约定一致，未知类型回退"其他"） */
const TYPE_LABELS: Record<string, string> = {
  title_unclear: "标题不清",
  text_overlap: "文字遮挡",
  image_cropped: "图片裁切",
  image_text_overlap: "图文重叠",
  text_too_small: "字体过小",
  too_much_whitespace: "留白过多",
  color_mismatch: "配色不当",
  theme_mismatch: "主题不符",
  other: "其他",
};

/** severity 中文标签与配色档位 */
const SEVERITY_LABELS: Record<string, string> = {
  high: "高",
  medium: "中",
  low: "低",
};

/** 总分配色：≥85 绿 / 70-84 橙 / <70 红（对齐 ReviewCard 的 pass/warn 色系） */
function scoreClass(score: number | undefined): string {
  if (typeof score !== "number") return "na";
  if (score >= 85) return "good";
  if (score >= 70) return "mid";
  return "bad";
}

/**
 * 视觉审查卡片（visual 帧）：渲染后逐页截图的视觉总分 + 按页分组问题清单。
 * available=false 时整卡降级为灰色提示条（如渲染器不可用）。
 */
export default function VisualReviewCard({ visual }: VisualReviewCardProps) {
  const unavailable = visual.available === false;

  if (unavailable) {
    return (
      <div className="visual-card unavailable">
        <span className="vc-off-icon">
          <EyeOff size={13} />
        </span>
        <span className="vc-reason">{visual.reason || "视觉审查不可用"}</span>
      </div>
    );
  }

  const issues = Array.isArray(visual.issues) ? visual.issues : [];
  // 按 page_id 分组（保持首次出现顺序；无页码归一组）
  const groups: { pageId: string; items: VisualIssue[] }[] = [];
  for (const it of issues) {
    const pid = typeof it?.page_id === "string" ? it.page_id : "";
    let g = groups.find((x) => x.pageId === pid);
    if (!g) {
      g = { pageId: pid, items: [] };
      groups.push(g);
    }
    g.items.push(it);
  }

  return (
    <div className={`visual-card ${scoreClass(visual.score)}`}>
      <div className="vc-head">
        <span className="vc-icon">
          <Eye size={13} />
        </span>
        <span className="vc-title">视觉审查</span>
        <span className={`vc-score ${scoreClass(visual.score)}`}>
          {typeof visual.score === "number" ? visual.score : "--"}
          <i>分</i>
        </span>
      </div>

      {groups.length > 0 ? (
        <ul className="vc-issues">
          {groups.map((g) => (
            <li key={g.pageId || "_"} className="vc-group">
              {g.pageId && <span className="vc-page">{g.pageId}</span>}
              <div className="vc-group-items">
                {g.items.map((it, j) => {
                  const sev = typeof it?.severity === "string" ? it.severity : "";
                  const type = typeof it?.type === "string" ? it.type : "";
                  return (
                    <div key={j} className="vc-issue">
                      <span className={`vc-sev ${sev || "low"}`}>
                        {SEVERITY_LABELS[sev] ?? "低"}
                      </span>
                      <span className="vc-type">{TYPE_LABELS[type] ?? TYPE_LABELS.other}</span>
                      {it?.suggestion && <span className="vc-sug">{it.suggestion}</span>}
                    </div>
                  );
                })}
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <div className="vc-clean">未发现视觉问题</div>
      )}
    </div>
  );
}
