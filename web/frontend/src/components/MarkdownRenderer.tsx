import { useState, isValidElement, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { Check, Copy } from "lucide-react";

export interface MarkdownRendererProps {
  content: string;
}

/**
 * 从 React 子树里提取纯文本。
 * rehype-highlight 会把代码块 children 加工成 <span> 元素数组（高亮 token），
 * 直接 String(children) 会得到 "[object Object],…" —— 必须递归取 text。
 */
function nodeText(node: ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (isValidElement(node)) return nodeText((node.props as { children?: ReactNode }).children);
  return "";
}

/** Markdown 渲染：精致排版 + 代码块（语言标签/复制/语法高亮）。 */
export default function MarkdownRenderer({ content }: MarkdownRendererProps) {
  return (
    <div className="prose-custom">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          code({ className, children }) {
            const isBlock = /language-/.test(className || "");
            if (!isBlock) {
              return (
                <code className={className}>
                  {children}
                </code>
              );
            }
            const match = /language-(\w+)/.exec(className || "");
            const lang = match ? match[1] : "code";
            // children 原样渲染（保留高亮 span），复制用提取出的纯文本
            return (
              <CodeBlock lang={lang} text={nodeText(children)}>
                {children}
              </CodeBlock>
            );
          },
          pre({ children }) {
            return <>{children}</>;
          },
          a({ children, href }) {
            return (
              <a href={href} target="_blank" rel="noreferrer">
                {children}
              </a>
            );
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

/** 代码块：顶部栏（语言标签 + 复制按钮）+ 语法高亮正文。 */
function CodeBlock({ lang, text, children }: { lang: string; text: string; children: ReactNode }) {
  const [copied, setCopied] = useState(false);
  function copy() {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }
  return (
    <div className="my-3 rounded-lg overflow-hidden border border-[var(--line)] bg-[var(--sunken)]">
      <div className="flex items-center justify-between px-3 py-1.5 bg-[var(--panel)] border-b border-[var(--line)]">
        <span className="text-[11px] text-[var(--text3)] font-mono">{lang}</span>
        <button
          onClick={copy}
          className="flex items-center gap-1 text-[11px] text-[var(--text3)] hover:text-[var(--text2)] transition"
        >
          {copied ? <Check size={12} /> : <Copy size={12} />}
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre className="overflow-x-auto px-4 py-3 text-[13px] leading-relaxed max-h-[420px] overflow-y-auto">
        <code className={`hljs language-${lang}`}>{children}</code>
      </pre>
    </div>
  );
}

export function InlineCode({ children }: { children: ReactNode }) {
  return <code className="bg-[var(--sunken)] px-1.5 py-0.5 rounded text-[var(--ok)] font-mono text-[0.85em]">{children}</code>;
}
