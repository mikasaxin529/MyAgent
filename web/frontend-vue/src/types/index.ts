// ChatFlow 式结构化消息类型（对齐 ChatFlow frontend types/index.ts，裁 DB/沙箱）。
// 后端 SSE 帧经 useChat dispatch 写入这些结构，MessageItem.vue 据结构渲染块。

export interface SearchItem {
  url: string
  title: string
  snippet: string
}

export type ToolDisplayMode = 'sources' | 'fetch' | 'text'

export interface ToolCallRecord {
  name: string
  input: Record<string, unknown>
  displayMode: ToolDisplayMode
  output: string
  searchItems: SearchItem[]
  done: boolean
}

export type ThinkingPhase = 'reasoning' | 'content'

export interface ThinkingSegment {
  node: string
  stepIndex: number | null
  phase: ThinkingPhase
  content: string
}

export type StepStatus = 'pending' | 'running' | 'done'

export interface StepRecord {
  index: number
  title: string
  description: string
  status: StepStatus
  content: string
  toolCalls: ToolCallRecord[]
  thinkingSegments: ThinkingSegment[]
}

export interface Message {
  role: 'user' | 'assistant'
  content: string
  steps: StepRecord[]
  // 消息级思考（route/planner/reflector 等非步骤节点，step_index=null）。
  thinkingSegments: ThinkingSegment[]
  route?: string
  done: boolean
  error?: string
  meta?: { audit_total: number }
}

// SSE node → 思考块标题映射（对齐 ChatFlow NODE_LABEL）。
export const NODE_LABEL: Record<string, string> = {
  route_model: '路由判断',
  planner: '规划',
  call_model: '推理',
  call_model_after_tool: '综合推理',
  reflector: '反思',
  system: '系统',
  '': '思考',
}
