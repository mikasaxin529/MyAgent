import { useState } from "react";
import { ChevronRight, Brain } from "lucide-react";
import type { StepItem } from "../api";
import { downloadFile } from "../api";
import FileCard from "./FileCard";

export interface ThoughtBlockProps {
  reasoning: string;
  steps: StepItem[];
  files: import("../api").FileItem[];
  identityColor?: string;
}

/** 思考过程可折叠区：图标 + reasoning 文本 + steps 时间线。 */
export default function ThoughtBlock({ reasoning, steps, files }: ThoughtBlockProps) {
  const [open, setOpen] = useState(false);
  const hasSteps = steps.length > 0;
  return (
    <div className="w-full">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 text-xs text-[var(--text3)] hover:text-[var(--text2)] transition"
        style={{ background: "transparent", border: "none", cursor: "pointer", fontFamily: "inherit" }}
      >
        <ChevronRight size={13} className={`transition-transform ${open ? "rotate-90" : ""}`} />
        <Brain size={13} />
        思考过程
        {hasSteps && <span className="text-[var(--text3)]">({steps.length} 步)</span>}
      </button>
      {open && (
        <div className="mt-1.5 px-3 py-2 rounded-lg bg-[var(--panel)] border border-[var(--line)] space-y-2">
          {reasoning && (
            <p className="whitespace-pre-wrap text-[var(--text3)] italic text-xs leading-relaxed" style={{ margin: 0 }}>
              {reasoning}
            </p>
          )}
          {hasSteps && (
            <ol className="tl" style={{ margin: 0 }}>
              {steps.map((s, i) => (
                <li key={i} style={{ paddingBottom: 6 }}>
                  <span className="dot done">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.2" strokeLinecap="round" strokeLinejoin="round" style={{ width: 10, height: 10 }}>
                      <path d="m5 12.5 5 5 9-11" />
                    </svg>
                  </span>
                  <div>
                    <div className="st">
                      <b>{s.label}</b>
                    </div>
                    {s.detail && <div className="out">{s.detail}</div>}
                  </div>
                </li>
              ))}
            </ol>
          )}
          {files.length > 0 && (
            <div className="files" style={{ marginTop: 4 }}>
              {files.map((f, i) => (
                <FileCard key={i} file={f} onDownload={downloadFile} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}