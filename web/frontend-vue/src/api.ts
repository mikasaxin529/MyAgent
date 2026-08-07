// DevPilot Vue 前端 API 客户端。
// 契约照搬自 web/frontend/src/api.ts（后端零改动），仅把实现用于 Vue 环境。
// WS 用 WebSocket（后端是 WS 非 SSE，故不搬 ChatFlow 的 fetch ReadableStream 解析）。

// ---- REST 响应类型 ----

export interface HealthInfo {
  providers: string[]
  primary: string
  fallback: string
  chain: string[]
}

export interface ChatResult {
  content: string
  provider: string
  model: string
  latency_ms: number
  prompt_tokens: number
  completion_tokens: number
}

export interface SkillSpec {
  name: string
  description: string
  schema: Record<string, { type: string }>
}

export interface Skill {
  name: string
  specs: SkillSpec[]
}

export interface EvalResult {
  accuracy: number
  robustness: number
  task_completion_rate: number
  avg_latency_ms: number
  avg_token_cost: number
  per_tag: Record<string, { accuracy: number; count: number }>
}

// ---- REST ----

async function jsonOrThrow<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`HTTP ${res.status}: ${text}`)
  }
  return (await res.json()) as T
}

export async function getHealth(): Promise<HealthInfo> {
  const res = await fetch('/api/health')
  return jsonOrThrow<HealthInfo>(res)
}

export async function postChat(prompt: string): Promise<ChatResult> {
  const res = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt }),
  })
  return jsonOrThrow<ChatResult>(res)
}

export async function getSkills(): Promise<Skill[]> {
  const res = await fetch('/api/skills')
  return jsonOrThrow<Skill[]>(res)
}

export async function postEval(): Promise<EvalResult> {
  const res = await fetch('/api/eval', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  })
  return jsonOrThrow<EvalResult>(res)
}

// ---- /ws/run 帧协议（Run 页 dev 全链路 + 审批握手） ----

export interface AuditEntry {
  timestamp: string
  event: 'agent_step' | 'llm_call' | 'tool_call' | 'approval' | string
  actor: string
  detail: Record<string, unknown>
  trace_id: string
}

export interface BlackboardData {
  plan: string[]
  code_diff: string
  review: string
  test_result: string
  artifacts: Record<string, unknown>
}

export interface ApprovalRequest {
  action: string
  args: Record<string, unknown>
  reason: string
}

export interface DoneSummary {
  total_events: number
  by_event: Record<string, number>
}

export type ServerFrame =
  | { type: 'audit'; entry: AuditEntry }
  | { type: 'blackboard'; data: BlackboardData }
  | { type: 'approval_request'; action: string; args: Record<string, unknown>; reason: string }
  | { type: 'done'; blackboard: BlackboardData; summary: DoneSummary }

export interface ClientTaskMessage {
  task: string
}

export interface ClientDecisionMessage {
  decision: 'approve' | 'reject' | 'edit'
  comment: string
  args: Record<string, unknown>
}

export type ClientMessage = ClientTaskMessage | ClientDecisionMessage

export type FrameHandler = (frame: ServerFrame) => void

// ---- /ws/chat 帧协议（ChatGPT 式流式聊天 + 图形流） ----

export interface ChatTokenFrame { type: 'token'; delta: string }
export interface ChatReasoningFrame { type: 'reasoning'; delta: string }
export interface ChatRouteFrame { type: 'route'; route: string; reason: string }
export interface ChatPlanFrame {
  type: 'plan'
  steps: {
    id: string
    name: string
    description: string
    output: string
    needs_search: boolean
    search_query?: string
    trust_memory?: boolean
  }[]
}
export interface ChatNodeFrame { type: 'node'; node_id: string; status: 'running' | 'done' }
export interface ChatBlackboardFrame { type: 'blackboard'; data: Partial<BlackboardData> }
export interface ChatStepFrame { type: 'step'; step: { kind: string; [k: string]: unknown } }
export interface ChatDoneFrame { type: 'done'; answer: string; meta: { nodes_visited: string[]; audit_total: number } }
export interface ChatErrorFrame { type: 'error'; message: string }

export type ChatFrame =
  | ChatTokenFrame
  | ChatReasoningFrame
  | ChatRouteFrame
  | ChatPlanFrame
  | ChatNodeFrame
  | ChatBlackboardFrame
  | ChatStepFrame
  | ChatDoneFrame
  | ChatErrorFrame

export type ChatFrameHandler = (frame: ChatFrame) => void

/** WS 客户端：连 /ws/chat，驱动 ChatGPT 式流式聊天 + 右侧图形流。 */
export class ChatSocket {
  private ws: WebSocket | null = null
  private handler: ChatFrameHandler
  private onOpen: () => void
  private onClose: () => void
  private onError: (err: Event) => void

  constructor(opts: {
    onFrame: ChatFrameHandler
    onOpen?: () => void
    onClose?: () => void
    onError?: (err: Event) => void
  }) {
    this.handler = opts.onFrame
    this.onOpen = opts.onOpen ?? (() => undefined)
    this.onClose = opts.onClose ?? (() => undefined)
    this.onError = opts.onError ?? (() => undefined)
  }

  connect(): void {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${proto}//${window.location.host}/ws/chat`
    const ws = new WebSocket(url)
    this.ws = ws
    ws.onopen = () => this.onOpen()
    ws.onclose = () => this.onClose()
    ws.onerror = (e) => this.onError(e)
    ws.onmessage = (ev) => {
      try {
        const frame = JSON.parse(ev.data) as ChatFrame
        this.handler(frame)
      } catch (err) {
        console.error('Failed to parse chat frame:', err)
      }
    }
  }

  sendPrompt(prompt: string, history?: { role: string; content: string }[]): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      console.warn('ChatSocket: send while not open')
      return
    }
    this.ws.send(JSON.stringify({ prompt, history: history ?? [] }))
  }

  close(): void {
    this.ws?.close()
    this.ws = null
  }
}

/** WS 客户端：连 /ws/run，驱动 dev 全链路 + 审批握手。 */
export class RunSocket {
  private ws: WebSocket | null = null
  private handler: FrameHandler
  private onOpen: () => void
  private onClose: () => void
  private onError: (err: Event) => void

  constructor(opts: {
    onFrame: FrameHandler
    onOpen?: () => void
    onClose?: () => void
    onError?: (err: Event) => void
  }) {
    this.handler = opts.onFrame
    this.onOpen = opts.onOpen ?? (() => undefined)
    this.onClose = opts.onClose ?? (() => undefined)
    this.onError = opts.onError ?? (() => undefined)
  }

  connect(): void {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${proto}//${window.location.host}/ws/run`
    const ws = new WebSocket(url)
    this.ws = ws

    ws.onopen = () => this.onOpen()
    ws.onclose = () => this.onClose()
    ws.onerror = (e) => this.onError(e)
    ws.onmessage = (ev) => {
      try {
        const frame = JSON.parse(ev.data) as ServerFrame
        this.handler(frame)
      } catch (err) {
        console.error('Failed to parse server frame:', err)
      }
    }
  }

  send(msg: ClientMessage): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      console.warn('RunSocket: send while not open')
      return
    }
    this.ws.send(JSON.stringify(msg))
  }

  close(): void {
    this.ws?.close()
    this.ws = null
  }
}

// ---- /api/chat SSE 帧协议（ChatFlow 式细粒度帧） ----
// 后端 POST /api/chat 返 text/event-stream，每行 data: {json}。
// 前端 fetch + reader.read() 逐行解析，按 type dispatch 到 onXxx 回调。

export interface SseStepPayload {
  id: string
  title: string
  description: string
  status: string
  result: string
}
export interface SseSearchItem {
  url: string
  title: string
  snippet: string
}

export interface SseThinkingFrame { type: 'thinking'; node: string; phase: 'reasoning' | 'content'; delta: string; step_index?: number }
export interface SseContentFrame { type: 'content'; delta: string; step_index: number }
export interface SseRouteFrame { type: 'route'; route: string; tool_model: string; answer_model: string }
export interface SsePlanFrame { type: 'plan'; steps: SseStepPayload[] }
export interface SseNodeFrame { type: 'node'; node_id: string; status: 'running' | 'done' }
export interface SseToolCallStartFrame { type: 'tool_call_start'; name: string; step_index: number }
export interface SseToolCallArgsFrame { type: 'tool_call_args'; text: string; step_index: number }
export interface SseToolCallFrame { type: 'tool_call'; name: string; input: Record<string, unknown>; display_mode: string }
export interface SseToolResultFrame { type: 'tool_result'; name: string; output: string; search_items: SseSearchItem[] }
export interface SseSearchItemFrame { type: 'search_item'; name: string; url: string; title: string; snippet: string }
export interface SseReflectionFrame { type: 'reflection'; content: string; decision: string }
export interface SseStatusFrame { type: 'status'; status: string }
export interface SseMemoryFrame { type: 'memory'; kind: string; count?: number }
export interface SseDoneFrame { type: 'done'; answer: string; meta: { audit_total: number } }
export interface SseErrorFrame { type: 'error'; message: string }

export type SseFrame =
  | SseThinkingFrame | SseContentFrame | SseRouteFrame | SsePlanFrame
  | SseNodeFrame | SseToolCallStartFrame | SseToolCallArgsFrame
  | SseToolCallFrame | SseToolResultFrame | SseSearchItemFrame
  | SseReflectionFrame | SseStatusFrame | SseMemoryFrame
  | SseDoneFrame | SseErrorFrame

export interface SseCallbacks {
  onThinking?: (node: string, phase: 'reasoning' | 'content', delta: string, stepIndex: number | null) => void
  onContent?: (delta: string, stepIndex: number) => void
  onRoute?: (route: string, toolModel: string, answerModel: string) => void
  onPlan?: (steps: SseStepPayload[]) => void
  onNode?: (nodeId: string, status: 'running' | 'done') => void
  onToolCallStart?: (name: string, stepIndex: number) => void
  onToolCallArgs?: (text: string, stepIndex: number) => void
  onToolCall?: (name: string, input: Record<string, unknown>, displayMode: string) => void
  onToolResult?: (name: string, output: string, searchItems: SseSearchItem[]) => void
  onSearchItem?: (name: string, item: SseSearchItem) => void
  onReflection?: (content: string, decision: string) => void
  onMemory?: (kind: string, count?: number) => void
  onDone?: (answer: string, meta: { audit_total: number }) => void
  onError?: (message: string) => void
}

/** SSE 客户端：fetch + ReadableStream 解析 data:{json} 行，按 type dispatch 回调。 */
export class ChatSseClient {
  private controller: AbortController | null = null
  private idleTimer: number | null = null
  private readonly idleTimeout = 300000 // 5 分钟无数据超时

  async sendMessage(
    prompt: string,
    history: { role: string; content: string }[],
    cb: SseCallbacks,
  ): Promise<void> {
    this.controller = new AbortController()
    let res: Response
    try {
      res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt, history }),
        signal: this.controller.signal,
      })
    } catch (e) {
      if ((e as Error).name !== 'AbortError') cb.onError?.((e as Error).message)
      return
    }
    if (!res.ok || !res.body) {
      cb.onError?.(`HTTP ${res.status}`)
      return
    }
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    this.resetIdle(cb)
    try {
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''
        for (const line of lines) {
          this.resetIdle(cb)
          if (!line.startsWith('data: ')) continue
          const json = line.slice(6).trim()
          if (!json) continue
          try {
            this.dispatch(JSON.parse(json) as SseFrame, cb)
          } catch (e) {
            console.error('parse sse frame failed:', e)
          }
        }
      }
    } catch (e) {
      if ((e as Error).name !== 'AbortError') cb.onError?.((e as Error).message)
    } finally {
      if (this.idleTimer) window.clearTimeout(this.idleTimer)
    }
  }

  private resetIdle(cb: SseCallbacks): void {
    if (this.idleTimer) window.clearTimeout(this.idleTimer)
    this.idleTimer = window.setTimeout(() => cb.onError?.('空闲超时'), this.idleTimeout)
  }

  private dispatch(frame: SseFrame, cb: SseCallbacks): void {
    switch (frame.type) {
      case 'thinking': cb.onThinking?.(frame.node, frame.phase, frame.delta, frame.step_index ?? null); break
      case 'content': cb.onContent?.(frame.delta, frame.step_index); break
      case 'route': cb.onRoute?.(frame.route, frame.tool_model, frame.answer_model); break
      case 'plan': cb.onPlan?.(frame.steps); break
      case 'node': cb.onNode?.(frame.node_id, frame.status); break
      case 'tool_call_start': cb.onToolCallStart?.(frame.name, frame.step_index); break
      case 'tool_call_args': cb.onToolCallArgs?.(frame.text, frame.step_index); break
      case 'tool_call': cb.onToolCall?.(frame.name, frame.input, frame.display_mode); break
      case 'tool_result': cb.onToolResult?.(frame.name, frame.output, frame.search_items); break
      case 'search_item': cb.onSearchItem?.(frame.name, { url: frame.url, title: frame.title, snippet: frame.snippet }); break
      case 'reflection': cb.onReflection?.(frame.content, frame.decision); break
      case 'memory': cb.onMemory?.(frame.kind, frame.count); break
      case 'done': cb.onDone?.(frame.answer, frame.meta); break
      case 'error': cb.onError?.(frame.message); break
      case 'status': break // 状态标签暂不展示
    }
  }

  abort(): void {
    this.controller?.abort()
    this.controller = null
  }
}

// ---- 共享类型（供 FlowGraph / useChat 复用） ----

export interface PlanStep {
  id: string
  name: string
  description: string
  output: string
  needs_search: boolean
  search_query?: string
  trust_memory?: boolean
}

export type NodeStatus = 'idle' | 'running' | 'done' | 'error'

export interface NodeUpdate {
  node_id: string
  status: NodeStatus
}
