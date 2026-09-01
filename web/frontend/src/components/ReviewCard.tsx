import { ShieldCheck, ShieldAlert } from "lucide-react";
import type { ReviewData } from "../api";

export interface ReviewCardProps {
  review: ReviewData;
}

/** 四维评分定义：key 与后端 scores 字段一致 */
const SCORE_DIMS: { key: keyof NonNullable<ReviewData["scores"]>; label: string }[] = [
  { key: "structure", label: "结构完整" },
  { key: "pedagogy", label: "教学逻辑" },
  { key: "content", label: "内容质量" },
  { key: "stage_fit", label: "学段适配" },
];

/** AI 审查卡片：四维星级 + 通过徽章 + 按页问题列表。刻意比大纲卡低调（过程性信息）。 */
export default function ReviewCard({ review }: ReviewCardProps) {
  const scores = review.scores ?? {};
  const issues = Array.isArray(review.issues)
    ? review.issues.filter((it) => Array.isArray(it.problems) && it.problems.length > 0)
    : [];
  const pass = review.pass === true;

  return (
    <div className={`review-card ${pass ? "pass" : "warn"}`}>
      <div className="rc-head">
        <span className={`rc-badge ${pass ? "pass" : "warn"}`}>
          {pass ? <ShieldCheck size={12} /> : <ShieldAlert size={12} />}
          {pass ? "审查通过" : "需要修订"}
        </span>
        <span className="rc-title">AI 审查</span>
      </div>

      <div className="rc-scores">
        {SCORE_DIMS.map((d) => {
          const v = typeof scores[d.key] === "number" ? scores[d.key]! : 0;
          const n = Math.max(0, Math.min(5, Math.round(v)));
          return (
            <div key={d.key} className="rc-dim">
              <span className="rc-dim-label">{d.label}</span>
              <span className="rc-stars" aria-label={`${n} / 5`}>
                {[1, 2, 3, 4, 5].map((i) => (
                  <i key={i} className={i <= n ? "on" : ""}>★</i>
                ))}
              </span>
              <span className="rc-num">{n}/5</span>
            </div>
          );
        })}
      </div>

      {issues.length > 0 && (
        <ul className="rc-issues">
          {issues.map((it, i) => (
            <li key={it.page_id ?? i}>
              {it.page_id && <span className="rc-page">{it.page_id}</span>}
              {(it.problems ?? []).map((p, j) => (
                <span key={j} className="rc-problem">{p}</span>
              ))}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
