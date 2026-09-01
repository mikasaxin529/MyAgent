// ---- Types ----

export interface AgentManifest {
  id: string;
  display_name: string;
  description: string;
  identity_color: string;
  placeholder: string;
}

export interface StepItem {
  id: string;
  label: string;
  status: "pending" | "running" | "done" | "error";
  ts: number;
  detail?: string;
}

export interface FileItem {
  name: string;
  path: string;
  size: number;
  mime: string;
}

/** 大纲页条目（outline 帧 pages[]，字段全可选防御） */
export interface OutlinePage {
  id?: string;
  kind?: string;
  title?: string;
  period?: number;
  points?: string;
}

/** 大纲元信息（outline 帧 meta） */
export interface OutlineMeta {
  title?: string;
  grade?: number;
  lessonType?: string;
  textbook?: string;
  periods?: number;
  theme?: string;
}

/** outline 帧 payload：课件大纲，等用户确认 */
export interface OutlineData {
  meta?: OutlineMeta;
  pages?: OutlinePage[];
}

/** review 帧四维评分（1-5） */
export interface ReviewScores {
  structure?: number;
  pedagogy?: number;
  content?: number;
  stage_fit?: number;
}

/** review 帧单页问题 */
export interface ReviewIssue {
  page_id?: string;
  problems?: string[];
}

/** review 帧 payload：AI 审查结果 */
export interface ReviewData {
  scores?: ReviewScores;
  issues?: ReviewIssue[];
  pass?: boolean;
}

export interface Message {
  role: "user" | "assistant";
  content: string;
  reasoning: string;
  steps: StepItem[];
  files: FileItem[];
  done: boolean;
  meta?: { nodes_visited: string[]; audit_total: number };
  error?: string;
  ts: number;
  /** 追问轮快捷选项（content/token 帧可选携带） */
  chips?: string[];
  /** 课件大纲（outline 帧，仅 yuwen 管线产出） */
  outline?: OutlineData;
  /** AI 审查结果（review 帧，仅 yuwen 管线产出） */
  review?: ReviewData;
}

// ---- Sessions（服务端持久化：SQLite via /api/sessions）----
// 迁移说明：会话原存 localStorage（dp_sessions），换设备/清缓存即丢。现走后端
// SQLite（.devpilot/store.db），多浏览器同源共享。localStorage 版本仍在：
// 首次打开时一次性迁移到服务端（migrateLocalSessions）。

export interface SessionItem {
  id: string;
  title: string;
  messages: Message[];
  updatedAt: number;
}

export interface SessionItem {
  id: string;
  title: string;
  messages: Message[];
  updatedAt: number;
}

export interface SessionSummary {
  id: string;
  title: string;
  updatedAt: number;
}

export interface SessionGroup {
  agentId: string;
  displayName: string;
  identityColor: string;
  sessions: SessionSummary[];
}

/** 服务端会话摘要行（GET /api/sessions 返回）。 */
export interface StoredSession {
  id: string;
  agent_id: string;
  title: string;
  created_at: number;
  updated_at: number;
}

const STORAGE_KEY = "dp_sessions";
const MIGRATED_KEY = "dp_sessions_migrated";

/** 老用户一次性迁移：localStorage 会话推到服务端，成功后打标不再重复。 */
export async function migrateLocalSessions(): Promise<number> {
  try {
    if (localStorage.getItem(MIGRATED_KEY)) return 0;
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) {
      localStorage.setItem(MIGRATED_KEY, "1");
      return 0;
    }
    const store = JSON.parse(raw) as Record<string, { sessions: SessionItem[] }>;
    let count = 0;
    for (const [agentId, group] of Object.entries(store)) {
      for (const sess of group.sessions ?? []) {
        await putSession(sess.id, agentId, sess.title ?? "新对话", sess.messages ?? []);
        count++;
      }
    }
    localStorage.setItem(MIGRATED_KEY, "1");
    return count;
  } catch {
    return 0; // 迁移失败下次再试（不打标）
  }
}

/** 列出服务端全部会话摘要。失败返回空列表（离线降级）。 */
export async function fetchStoredSessions(): Promise<StoredSession[]> {
  try {
    const res = await fetch("/api/sessions");
    if (!res.ok) return [];
    const data = (await res.json()) as { sessions: StoredSession[] };
    return data.sessions ?? [];
  } catch {
    return [];
  }
}

/** 取整条会话（含消息）。404/网络失败返回 null。 */
export async function fetchStoredSession(sessionId: string): Promise<SessionItem | null> {
  try {
    const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`);
    if (!res.ok) return null;
    const data = (await res.json()) as {
      session: { id: string; title: string; updated_at: number; messages: Message[] };
    };
    return {
      id: data.session.id,
      title: data.session.title,
      updatedAt: data.session.updated_at,
      messages: data.session.messages ?? [],
    };
  } catch {
    return null;
  }
}

/** 整段 upsert 会话（fire-and-forget，失败静默——下次落盘覆盖）。 */
export async function putSession(
  sessionId: string,
  agentId: string,
  title: string,
  messages: Message[],
): Promise<void> {
  try {
    await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agent: agentId, title, messages }),
    });
  } catch {
    // 网络失败静默：流结束后重试成本高，丢一次落盘可接受
  }
}

/** 删除会话。失败静默（列表刷新时会再现，用户可重删）。 */
export async function deleteStoredSession(sessionId: string): Promise<void> {
  try {
    await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
  } catch {
    // ignore
  }
}

// ---- 用户偏好（记忆上次智能体） ----

const LAST_AGENT_KEY = "dp_last_agent";

export function getLastAgent(): string | null {
  try {
    return localStorage.getItem(LAST_AGENT_KEY);
  } catch {
    return null;
  }
}

export function setLastAgent(agentId: string): void {
  try {
    localStorage.setItem(LAST_AGENT_KEY, agentId);
  } catch {
    // ignore
  }
}

// ---- 交付物下载 / 预览 ----

/** 触发浏览器下载（后端 /files 默认发 Content-Disposition: attachment）。 */
export function downloadFile(path: string): void {
  window.open(path, "_blank", "noopener");
}

/** HTML 课件浏览器内预览：?inline=1 让后端不发 attachment。 */
export function previewFile(path: string): void {
  window.open(path + (path.includes("?") ? "&" : "?") + "inline=1", "_blank", "noopener");
}

// ---- Theme storage ----

const THEME_KEY = "dp_theme";

export function getStoredTheme(): "dark" | "light" | null {
  try {
    const v = localStorage.getItem(THEME_KEY);
    if (v === "dark" || v === "light") return v;
  } catch {
    // ignore
  }
  return null;
}

export function setStoredTheme(theme: "dark" | "light"): void {
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    // ignore
  }
}

// ---- REST ----

export async function fetchAgents(baseUrl?: string): Promise<AgentManifest[]> {
  const res = await fetch(`${baseUrl ?? ""}/api/agents`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  return data.agents as AgentManifest[];
}

// ---- Legacy REST endpoints (Run / Eval / Skills pages) ----

export interface SkillSpec {
  name: string;
  description: string;
  schema: Record<string, { type: string }>;
}

export interface Skill {
  name: string;
  specs: SkillSpec[];
}

export async function getSkills(): Promise<Skill[]> {
  const res = await fetch("/api/skills");
  return jsonOrThrow<Skill[]>(res);
}

export interface EvalResult {
  accuracy: number;
  robustness: number;
  task_completion_rate: number;
  avg_latency_ms: number;
  avg_token_cost: number;
  per_tag: Record<string, { accuracy: number; count: number }>;
}

export async function postEval(): Promise<EvalResult> {
  const res = await fetch("/api/eval", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  return jsonOrThrow<EvalResult>(res);
}

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status}: ${text}`);
  }
  return (await res.json()) as T;
}

// ---- Legacy WS (Run page) ----

export interface AuditEntry {
  timestamp: string;
  event: "agent_step" | "llm_call" | "tool_call" | "approval" | string;
  actor: string;
  detail: Record<string, unknown>;
  trace_id: string;
}

export interface BlackboardData {
  plan: string[];
  code_diff: string;
  review: string;
  test_result: string;
  artifacts: Record<string, unknown>;
}

export interface ApprovalRequest {
  action: string;
  args: Record<string, unknown>;
  reason: string;
}

export interface DoneSummary {
  total_events: number;
  by_event: Record<string, number>;
}

export type ServerFrame =
  | { type: "audit"; entry: AuditEntry }
  | { type: "blackboard"; data: BlackboardData }
  | { type: "approval_request"; action: string; args: Record<string, unknown>; reason: string }
  | { type: "done"; blackboard: BlackboardData; summary: DoneSummary };

export interface ClientTaskMessage {
  task: string;
}

export interface ClientDecisionMessage {
  decision: "approve" | "reject" | "edit";
  comment: string;
  args: Record<string, unknown>;
}

export type ClientMessage = ClientTaskMessage | ClientDecisionMessage;

export type FrameHandler = (frame: ServerFrame) => void;

/** A thin wrapper around WebSocket that enforces the DevPilot /ws/run protocol. */
export class RunSocket {
  private ws: WebSocket | null = null;
  private handler: FrameHandler;
  private onOpen: () => void;
  private onClose: () => void;
  private onError: (err: Event) => void;

  constructor(opts: {
    onFrame: FrameHandler;
    onOpen?: () => void;
    onClose?: () => void;
    onError?: (err: Event) => void;
  }) {
    this.handler = opts.onFrame;
    this.onOpen = opts.onOpen ?? (() => undefined);
    this.onClose = opts.onClose ?? (() => undefined);
    this.onError = opts.onError ?? (() => undefined);
  }

  connect(): void {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${window.location.host}/ws/run`;
    const ws = new WebSocket(url);
    this.ws = ws;

    ws.onopen = () => this.onOpen();
    ws.onclose = () => this.onClose();
    ws.onerror = (e) => this.onError(e);
    ws.onmessage = (ev) => {
      try {
        const frame = JSON.parse(ev.data) as ServerFrame;
        this.handler(frame);
      } catch (err) {
        console.error("Failed to parse server frame:", err);
      }
    };
  }

  send(msg: ClientMessage): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      console.warn("RunSocket: send while not open");
      return;
    }
    this.ws.send(JSON.stringify(msg));
  }

  close(): void {
    this.ws?.close();
    this.ws = null;
  }
}

// ---- SSE client (POST /api/chat → ReadableStream) ----

export interface SSEChatOptions {
  onToken?: (delta: string, stepId?: string, chips?: string[]) => void;
  onStep?: (step: StepItem) => void;
  onFiles?: (files: FileItem[]) => void;
  /** outline 帧：课件大纲（含可选确认 chips） */
  onOutline?: (outline: OutlineData, chips?: string[]) => void;
  /** review 帧：AI 审查结果 */
  onReview?: (review: ReviewData) => void;
  onAgentMeta?: (meta: {
    agent_id: string;
    display_name: string;
    description: string;
    identity_color: string;
    placeholder: string;
  }) => void;
  onError?: (message: string) => void;
  onDone?: (answer: string, meta: { nodes_visited: string[]; audit_total: number }) => void;
  onMessage?: (frame: Record<string, unknown>) => void;
  signal?: AbortSignal;
}

export async function chatSSE(
  prompt: string,
  history: { role: string; content: string }[],
  agent: string,
  opts: SSEChatOptions,
  sessionId?: string,
): Promise<void> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, history, agent, session_id: sessionId ?? "" }),
    signal: opts.signal,
  });
  if (!res.ok) {
    opts.onError?.(`HTTP ${res.status}`);
    return;
  }
  const reader = res.body?.getReader();
  if (!reader) {
    opts.onError?.("No response body");
    return;
  }

  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const jsonStr = line.slice(6);
        if (!jsonStr) continue;
        try {
          const frame = JSON.parse(jsonStr) as Record<string, unknown>;
          opts.onMessage?.(frame);
          switch (frame.type) {
            case "token":
            case "content":
              {
                const stepId = (frame.step_id as string | undefined) ?? (frame.step_index as string | undefined);
                const chips = Array.isArray(frame.chips) ? (frame.chips as string[]) : undefined;
                opts.onToken?.(frame.delta as string, stepId, chips);
              }
              break;
            case "step":
              opts.onStep?.(frame as unknown as StepItem);
              break;
            case "files":
              opts.onFiles?.(frame.files as FileItem[]);
              break;
            case "outline":
              opts.onOutline?.(
                (frame.outline ?? {}) as OutlineData,
                Array.isArray(frame.chips) ? (frame.chips as string[]) : undefined,
              );
              break;
            case "review":
              opts.onReview?.((frame.review ?? {}) as ReviewData);
              break;
            case "agent_meta":
              opts.onAgentMeta?.(frame as {
                agent_id: string;
                display_name: string;
                description: string;
                identity_color: string;
                placeholder: string;
              });
              break;
            case "error":
              opts.onError?.(frame.message as string);
              break;
            case "done":
              opts.onDone?.(
                frame.answer as string,
                frame.meta as { nodes_visited: string[]; audit_total: number },
              );
              break;
          }
        } catch {
          // skip malformed frames
        }
      }
    }
  } catch (err: unknown) {
    if (err instanceof DOMException && err.name === "AbortError") {
      return;
    }
    opts.onError?.(String(err));
  }
}