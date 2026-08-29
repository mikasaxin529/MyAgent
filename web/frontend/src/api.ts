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
}

// ---- Sessions local storage ----

export interface SessionItem {
  id: string;
  title: string;
  messages: Message[];
  updatedAt: number;
}

export interface AgentSessionGroup {
  activeIndex: number;
  sessions: SessionItem[];
}

export type SessionStore = Record<string, AgentSessionGroup>;

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

const STORAGE_KEY = "dp_sessions";

export function loadSessions(): SessionStore {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    return JSON.parse(raw) as SessionStore;
  } catch {
    return {};
  }
}

export function saveSessions(store: SessionStore): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(store));
  } catch {
    // storage full — silently ignore
  }
}

export function getAgentSessions(agentId: string): AgentSessionGroup {
  const store = loadSessions();
  return store[agentId] ?? { activeIndex: 0, sessions: [] };
}

export function saveAgentSession(agentId: string, group: AgentSessionGroup): void {
  const store = loadSessions();
  store[agentId] = group;
  saveSessions(store);
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
): Promise<void> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, history, agent }),
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