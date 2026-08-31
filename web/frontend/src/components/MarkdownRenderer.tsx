import { useState, useEffect, useRef, isValidElement, type ReactNode } from "react";
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

/** Markdown 渲染：精致排版 + 代码块（语言标签/复制/语法高亮）+ Mermaid 图表。 */
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
            const text = nodeText(children);
            // mermaid 代码块 → 画图，不当文本渲染
            if (lang === "mermaid") {
              return <MermaidBlock chart={text} />;
            }
            // children 原样渲染（保留高亮 span），复制用提取出的纯文本
            return (
              <CodeBlock lang={lang} text={text}>
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
          // 表格外包一层：宽表格横向滚动，不撑破消息列
          table({ children }) {
            return (
              <div className="md-table-wrap">
                <table>{children}</table>
              </div>
            );
          },
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

// ----------------------------------------------------------------------
// Mermaid 图表：\`\`\`mermaid 围栏渲染为 SVG 流程图/时序图/甘特图等。
// 业内主流（ChatGPT/千问/Kimi/Notion AI）对架构、流程类回答都给可视化图，
// 前提是 prompt 里告诉模型可以用 mermaid（见 cf/base.py 排版要求）。
// ----------------------------------------------------------------------
function MermaidBlock({ chart }: { chart: string }) {
  const [svg, setSvg] = useState("");
  const [error, setError] = useState("");
  const idRef = useRef(`mmd-${Math.random().toString(36).slice(2, 9)}`);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({
          startOnLoad: false,
          theme: document.documentElement.getAttribute("data-theme") === "dark"
            ? "dark" : "default",
          fontFamily: "inherit",
          // 跟随应用字号，避免图里文字过大
          fontSize: 13,
        });
        const { svg } = await mermaid.render(idRef.current, chart.trim());
        if (!cancelled) {
          setSvg(svg);
          setError("");
        }
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    })();
    return () => { cancelled = true; };
  }, [chart]);

  if (error) {
    // 渲染失败退化为普通代码块，内容不丢
    return (
      <CodeBlock lang="mermaid" text={chart}>
        <code>{chart}</code>
      </CodeBlock>
    );
  }
  if (!svg) {
    return (
      <div className="mermaid-box mermaid-loading">
        <span>图表生成中…</span>
      </div>
    );
  }
  return (
    <div
      className="mermaid-box"
      // mermaid.render 输出自家 SVG；内容源自本地模型输出（同 markdown 信任级别）
      dangerouslySetInnerHTML={{ __html: svg }}
    />
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
    <div className="codeblock">
      <div className="cb-head">
        <span className="cb-lang">{lang}</span>
        <button
          onClick={copy}
          className="cb-copy"
          title={copied ? "已复制" : "复制代码"}
        >
          {copied ? <Check size={13} /> : <Copy size={13} />}
          {copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre className="cb-pre">
        <code className={`hljs language-${lang}`}>{children}</code>
      </pre>
    </div>
  );
}

export function InlineCode({ children }: { children: ReactNode }) {
  return <code className="bg-[var(--sunken)] px-1.5 py-0.5 rounded text-[var(--ok)] font-mono text-[0.85em]">{children}</code>;
}
