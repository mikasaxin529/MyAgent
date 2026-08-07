// REST + WebSocket client for the DevPilot backend.
// Contract is shared with the FastAPI backend (see server implementation).

export interface HealthInfo {
  providers: string[];
  primary: string;
  fallback: string;
  chain: string[];
}

export interface ChatResult {
  content: string;
  provider: string;
  model: string;
  latency_ms: number;
  prompt_tokens: number;
  completion_tokens: number;
}

export interface SkillSpec {
  name: string;
  description: string;
  schema: Record<string, { type: string }>;
}

export interface Skill {
  name: string;
  specs: SkillSpec[];
}

export interface EvalResult {
  accuracy: number;
  robustness: number;
  task_completion_rate: number;
  avg_latency_ms: number;
  avg_token_cost: number;
  per_tag: Record<string, { accuracy: number; count: number }>;
}

// ---- REST ----

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status}: ${text}`);
  }
  return (await res.json()) as T;
}

export async function getHealth(): Promise<HealthInfo> {
  const res = await fetch("/api/health");
  return jsonOrThrow<HealthInfo>(res);
}

export async function postChat(prompt: string): Promise<ChatResult> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });
  return jsonOrThrow<ChatResult>(res);
}

export async function getSkills(): Promise<Skill[]> {
  const res = await fetch("/api/skills");
  return jsonOrThrow<Skill[]>(res);
}

export async function postEval(): Promise<EvalResult> {
  const res = await fetch("/api/eval", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  return jsonOrThrow<EvalResult>(res);
}

// ---- WebSocket ----

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

// ---- Chat WS 帧协议（/ws/chat，ChatGPT 式流式聊天 + 图形流） ----

export interface ChatTokenFrame { type: "token"; delta: string }
export interface ChatReasoningFrame { type: "reasoning"; delta: string }
export interface ChatRouteFrame { type: "route"; route: string; reason: string }
export interface ChatPlanFrame {
  type: "plan";
  steps: {
    id: string;
    name: string;
    description: string;
    output: string;
    needs_search: boolean;
    search_query?: string;
    trust_memory?: boolean;
  }[];
}
export interface ChatNodeFrame { type: "node"; node_id: string; status: "running" | "done" }
export interface ChatBlackboardFrame { type: "blackboard"; data: Partial<BlackboardData> }
export interface ChatStepFrame { type: "step"; step: { kind: string; [k: string]: unknown } }
export interface ChatDoneFrame { type: "done"; answer: string; meta: { nodes_visited: string[]; audit_total: number } }
export interface ChatErrorFrame { type: "error"; message: string }

export type ChatFrame =
  | ChatTokenFrame
  | ChatReasoningFrame
  | ChatRouteFrame
  | ChatPlanFrame
  | ChatNodeFrame
  | ChatBlackboardFrame
  | ChatStepFrame
  | ChatDoneFrame
  | ChatErrorFrame;

export type ChatFrameHandler = (frame: ChatFrame) => void;

/** WS 客户端：连 /ws/chat，驱动 ChatGPT 式流式聊天 + 右侧图形流。
 *  照搬 RunSocket 模式（单 onFrame + JSON 解析 + send/close）。 */
export class ChatSocket {
  private ws: WebSocket | null = null;
  private handler: ChatFrameHandler;
  private onOpen: () => void;
  private onClose: () => void;
  private onError: (err: Event) => void;

  constructor(opts: {
    onFrame: ChatFrameHandler;
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
    const url = `${proto}//${window.location.host}/ws/chat`;
    const ws = new WebSocket(url);
    this.ws = ws;
    ws.onopen = () => this.onOpen();
    ws.onclose = () => this.onClose();
    ws.onerror = (e) => this.onError(e);
    ws.onmessage = (ev) => {
      try {
        const frame = JSON.parse(ev.data) as ChatFrame;
        this.handler(frame);
      } catch (err) {
        console.error("Failed to parse chat frame:", err);
      }
    };
  }

  sendPrompt(prompt: string): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      console.warn("ChatSocket: send while not open");
      return;
    }
    this.ws.send(JSON.stringify({ prompt }));
  }

  close(): void {
    this.ws?.close();
    this.ws = null;
  }
}

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
