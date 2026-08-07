// Chat 页状态管理 + SSE 帧分发（ChatFlow 式 ConvState + onXxx 回调）。
// 后端 POST /api/chat 返 SSE，前端 ChatSseClient 解析后按 type 回调，
// 写入 Message（含 steps/thinkingSegments/toolCalls 等结构化块）。
import { ref } from 'vue'
import { ChatSseClient, type SseCallbacks } from '../api'
import type { Message, StepRecord, ToolCallRecord, ToolDisplayMode } from '../types'
import { activeStep, applyThinkingEvent } from './thinking'

export function useChat() {
  const prompt = ref('')
  const loading = ref(false)
  const messages = ref<Message[]>([])
  let client: ChatSseClient | null = null

  /** 对最后一条 assistant 消息就地更新（Vue 响应式直接 mutate）。 */
  function patchLast(fn: (m: Message) => void): void {
    const last = messages.value[messages.value.length - 1]
    if (last && last.role === 'assistant') fn(last)
  }

  const callbacks: SseCallbacks = {
    onThinking: (node, phase, delta, stepIndex) => {
      patchLast((m) => applyThinkingEvent(m, node, phase, delta, stepIndex))
    },
    onContent: (delta, stepIndex) => {
      patchLast((m) => {
        const step = m.steps[stepIndex] ?? activeStep(m)
        if (step) step.content += delta
        else m.content += delta
      })
    },
    onRoute: (route) => {
      patchLast((m) => { m.route = route })
    },
    onPlan: (steps) => {
      patchLast((m) => {
        m.steps = steps.map((s, i) => ({
          index: i,
          title: s.title,
          description: s.description,
          status: i === 0 ? 'running' : 'pending',
          content: '',
          toolCalls: [],
          thinkingSegments: [],
        } as StepRecord))
      })
    },
    onNode: (nodeId, status) => {
      patchLast((m) => {
        // stepN 节点 → 对应步骤状态
        const match = nodeId.match(/^step(\d+)$/)
        if (match) {
          const idx = parseInt(match[1], 10) - 1
          if (m.steps[idx]) m.steps[idx].status = status === 'done' ? 'done' : 'running'
        }
      })
    },
    onToolCallStart: (name, _stepIndex) => {
      patchLast((m) => {
        const step = activeStep(m)
        if (!step) return
        step.toolCalls.push({
          name, input: {}, displayMode: 'text', output: '', searchItems: [], done: false,
        })
      })
    },
    onToolCallArgs: () => {
      // 参数逐字增量暂不入结构（tool_call 帧会带完整 input），可忽略。
    },
    onToolCall: (name, input, displayMode) => {
      patchLast((m) => {
        const step = activeStep(m)
        if (!step) return
        const tc = step.toolCalls[step.toolCalls.length - 1]
        if (tc && tc.name === name && !tc.done) {
          tc.input = input
          tc.displayMode = displayMode as ToolDisplayMode
          tc.done = true
        } else {
          step.toolCalls.push({
            name, input, displayMode: displayMode as ToolDisplayMode,
            output: '', searchItems: [], done: true,
          } as ToolCallRecord)
        }
      })
    },
    onToolResult: (name, output, searchItems) => {
      patchLast((m) => {
        const step = activeStep(m)
        if (!step) return
        const tc = step.toolCalls.find((t) => t.name === name && t.done)
        if (tc) {
          tc.output = output
          if (searchItems.length) tc.searchItems = searchItems
        }
      })
    },
    onSearchItem: (name, item) => {
      patchLast((m) => {
        const step = activeStep(m)
        if (!step) return
        const tc = step.toolCalls.find((t) => t.name === name)
        if (tc) tc.searchItems.push(item)
      })
    },
    onReflection: (_content, decision) => {
      patchLast((m) => {
        // continue → 当前步 done，下一步 running；
        // done → 当前 running 步标 done（单步搜索的常见收尾，否则 badge 卡“进行中”）；
        // retry → 保持当前步 running（重试同一歩）。
        if (decision === 'continue') {
          const cur = activeStep(m)
          if (cur) cur.status = 'done'
          const nextIdx = (cur?.index ?? -1) + 1
          if (m.steps[nextIdx]) m.steps[nextIdx].status = 'running'
        } else if (decision === 'done') {
          for (const s of m.steps) if (s.status === 'running') s.status = 'done'
        }
      })
    },
    onDone: (answer) => {
      patchLast((m) => {
        m.content = m.content || answer
        m.done = true
        // 收尾：所有 running 步标 done，防止 reflector 漏 done 时 badge 卡“进行中”。
        for (const s of m.steps) if (s.status === 'running') s.status = 'done'
      })
      loading.value = false
      client = null
    },
    onError: (message) => {
      patchLast((m) => { m.error = message; m.done = true })
      loading.value = false
      client = null
    },
  }

  async function send(text: string): Promise<void> {
    if (!text || loading.value) return
    const history = messages.value
      .filter((m) => (m.role === 'user' || m.role === 'assistant') && m.content && !m.error)
      .map((m) => ({ role: m.role, content: m.content }))
      .slice(-30)
    messages.value.push({
      role: 'user', content: text, steps: [], thinkingSegments: [], done: false,
    })
    messages.value.push({
      role: 'assistant', content: '', steps: [], thinkingSegments: [], done: false,
    })
    prompt.value = ''
    loading.value = true
    client = new ChatSseClient()
    await client.sendMessage(text, history, callbacks)
  }

  function close(): void {
    client?.abort()
    client = null
  }

  return { prompt, loading, messages, send, close }
}
