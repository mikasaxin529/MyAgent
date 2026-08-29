import { Check, Ellipsis, Circle } from "lucide-react";
import type { StepItem, FileItem } from "../api";
import FileCard from "./FileCard";

export interface TrackedStep extends StepItem {
  duration?: number;
}

export interface TimelineProps {
  steps: TrackedStep[];
  files: FileItem[];
  totalTime?: string;
  doneCount?: string;
  isRunning?: boolean;
}

function dotIcon(status: StepItem["status"]) {
  switch (status) {
    case "done":
      return <Check size={12} />;
    case "running":
      return <Ellipsis size={12} />;
    case "error":
      return <span style={{ fontWeight: 700, fontSize: 11 }}>!</span>;
    default:
      return <Circle size={8} />;
  }
}

/** 右侧时间线面板：显示步骤进度 + 产物列表。 */
export default function Timeline({ steps, files, totalTime, doneCount, isRunning }: TimelineProps) {
  // Merge steps by id: keep first occurrence's ts, update status/detail/duration from later ones
  const stepMap = new Map<string, TrackedStep>();
  for (const s of steps) {
    const existing = stepMap.get(s.id);
    if (!existing) {
      stepMap.set(s.id, { ...s });
    } else {
      stepMap.set(s.id, {
        ...existing,
        status: s.status,
        ts: s.ts,
        detail: s.detail ?? existing.detail,
        duration: (s as TrackedStep).duration ?? existing.duration,
      });
    }
  }
  const uniqueSteps = Array.from(stepMap.values());
  const total = uniqueSteps.length;

  return (
    <>
      <div className="psec-t">
        <b>工作流</b>
        <span>
          {isRunning && doneCount ? `${doneCount} · ` : ""}
          {totalTime ?? `${total} 步`}
        </span>
      </div>
      {uniqueSteps.length === 0 ? (
        <div style={{ padding: "24px 0", textAlign: "center", color: "var(--text3)", fontSize: 12.5 }}>
          等待执行…
        </div>
      ) : (
        <ol className="tl">
          {uniqueSteps.map((s) => {
            const dotClass =
              s.status === "done" ? "done" : s.status === "running" ? "run" : s.status === "error" ? "error" : "pending";
            const ts = s as TrackedStep;
            return (
              <li key={s.id} className={s.status === "pending" ? "wait" : ""}>
                <span className={`dot ${dotClass}`}>{dotIcon(s.status)}</span>
                <div>
                  <div className="st">
                    <b>{s.label}</b>
                    {s.status === "done" && ts.duration != null && (
                      <time>{ts.duration < 0.1 ? "<0.1s" : `${ts.duration.toFixed(1)}s`}</time>
                    )}
                    {s.status === "running" && <time style={{ color: "var(--run)" }}>运行中…</time>}
                  </div>
                  {s.detail && <div className="out">{s.detail}</div>}
                </div>
              </li>
            );
          })}
        </ol>
      )}

      {files.length > 0 && (
        <>
          <div className="psec-t" style={{ marginTop: 8 }}>
            <b>产物</b>
            <span>{files.length} 个文件</span>
          </div>
          <div className="files">
            {files.map((f, i) => (
              <FileCard key={i} file={f} onDownload={(path) => { window.open(path, "_blank"); }} />
            ))}
          </div>
        </>
      )}
    </>
  );
}