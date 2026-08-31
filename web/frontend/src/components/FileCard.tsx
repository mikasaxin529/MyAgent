import { Download, Eye } from "lucide-react";
import type { FileItem } from "../api";
import { previewFile } from "../api";

export interface FileCardProps {
  file: FileItem;
  onDownload: (path: string) => void;
}

function fileTileClass(name: string): { cls: string; label: string } {
  const lower = name.toLowerCase();
  if (lower.endsWith(".pptx") || lower.endsWith(".ppt")) return { cls: "pptx", label: "PPT" };
  if (lower.endsWith(".html") || lower.endsWith(".htm")) return { cls: "html", label: "</>" };
  if (lower.endsWith(".docx") || lower.endsWith(".doc")) return { cls: "docx", label: "DOC" };
  return { cls: "", label: "FILE" };
}

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${bytes} B`;
}

/** 交付物下载卡片：类型色块 + 文件名 + 大小 + 下载按钮。 */
export default function FileCard({ file, onDownload }: FileCardProps) {
  const { cls, label } = fileTileClass(file.name);
  return (
    <div className="file">
      <span className={`tile ${cls}`}>{label}</span>
      <div className="fmeta">
        <b title={file.name}>{file.name}</b>
        <span>{formatSize(file.size)}</span>
      </div>
      {file.mime.startsWith("text/html") ? (
        <>
          <button className="dl ghost" onClick={() => previewFile(file.path)} title="预览">
            <Eye />
          </button>
          <button className="dl" onClick={() => onDownload(file.path)} title="下载">
            <Download />
          </button>
        </>
      ) : (
        <button className="dl" onClick={() => onDownload(file.path)} title="下载">
          <Download />
        </button>
      )}
    </div>
  );
}