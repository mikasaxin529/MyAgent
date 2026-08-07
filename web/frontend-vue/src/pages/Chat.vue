<script setup lang="ts">
// ChatGPT 式聊天页：左侧消息流 + 右侧动态图形流。
// SSE 流式（POST /api/chat）+ ChatFlow 式结构化消息块。
import { onUnmounted, ref, watch, nextTick, computed } from 'vue'
import { Brain, Send } from 'lucide-vue-next'
import { useChat } from '../composables/useChat'
import MessageItem from '../components/MessageItem.vue'
import FlowGraph from '../components/FlowGraph.vue'

const { prompt, loading, messages, send, close } = useChat()
const scrollRef = ref<HTMLElement | null>(null)

async function onSubmit(): Promise<void> {
  const text = prompt.value.trim()
  if (!text || loading.value) return
  await send(text)
}

// 自动滚到底：messages 变化（每 token）即触发。
watch(
  () => messages.value,
  () => {
    nextTick(() => {
      const el = scrollRef.value
      if (el) el.scrollTop = el.scrollHeight
    })
  },
  { deep: true },
)

// 从末条 assistant 消息派生 FlowGraph 的 plan + nodeUpdates。
const lastAssistant = computed(() => {
  for (let i = messages.value.length - 1; i >= 0; i--) {
    if (messages.value[i].role === 'assistant') return messages.value[i]
  }
  return null
})
const derivedPlan = computed(() => {
  const m = lastAssistant.value
  if (!m || !m.steps.length) return null
  return m.steps.map((s) => ({
    id: 'step' + (s.index + 1),
    name: s.title,
    description: s.description,
    output: '',
    needs_search: false,
  }))
})
const derivedUpdates = computed(() => {
  const m = lastAssistant.value
  if (!m) return []
  const ups: { node_id: string; status: 'idle' | 'running' | 'done' | 'error' }[] = []
  ups.push({ node_id: 'route_model', status: m.route ? 'done' : 'running' })
  ups.push({ node_id: 'planner', status: m.steps.length ? 'done' : 'running' })
  m.steps.forEach((s) => ups.push({ node_id: 'step' + (s.index + 1), status: s.status === 'pending' ? ('idle' as const) : s.status }))
  return ups
})

onUnmounted(() => close())
</script>

<template>
  <div class="chat-page">
    <!-- 左：聊天区 -->
    <div class="chat-col">
      <header class="chat-header">
        <div class="brand grad-bili-text">Chat</div>
        <span class="sub">动态编排 · 流式输出 · 思考过程</span>
      </header>

      <div ref="scrollRef" class="msg-list">
        <div v-if="messages.length === 0" class="empty">
          <Brain :size="32" />
          <span>输入需求，模型将动态规划步骤并执行</span>
        </div>
        <MessageItem v-for="(m, i) in messages" :key="i" :message="m" />
      </div>

      <form class="input-bar" @submit.prevent="onSubmit">
        <input
          v-model="prompt"
          placeholder="输入需求，如「帮我总结7月最新AI资讯」(Enter 发送)"
        />
        <button type="submit" :disabled="loading || !prompt.trim()">
          <template v-if="loading"><Brain :size="15" class="pulse" /> 生成中…</template>
          <template v-else><Send :size="15" /> 发送</template>
        </button>
      </form>
    </div>

    <!-- 右：Dify 式动态图形流 -->
    <div class="flow-col">
      <div class="flow-header">
        编排图 · {{ derivedPlan ? derivedPlan.length + ' 步计划' : '等待规划' }}
      </div>
      <div class="flow-body">
        <FlowGraph :plan="derivedPlan" :node-updates="derivedUpdates" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.chat-page {
  height: 100%;
  display: flex;
}
.chat-col {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.chat-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 24px;
  min-height: 52px;
  box-sizing: border-box;
  border-bottom: 1px solid var(--cf-border);
}
.brand {
  font-size: 18px;
  font-weight: 700;
}
.sub {
  font-size: 12px;
  color: var(--cf-text-3);
}
.msg-list {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.empty {
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: var(--cf-text-4);
  font-size: 13px;
}
.input-bar {
  display: flex;
  gap: 12px;
  align-items: center;
  padding: 16px 24px;
  border-top: 1px solid var(--cf-border);
}
.input-bar input {
  flex: 1;
  padding: 10px 16px;
  border-radius: 12px;
  background: var(--cf-card);
  border: 1px solid var(--cf-border);
  color: var(--cf-text-1);
  font-size: 14px;
}
.input-bar input:focus {
  outline: none;
  border-color: var(--cf-bili-blue);
  box-shadow: 0 0 0 2px rgba(0, 174, 236, 0.15);
}
.input-bar button {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 20px;
  border-radius: 12px;
  background: var(--cf-bili-blue);
  color: #fff;
  border: none;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s;
}
.input-bar button:hover:not(:disabled) {
  background: var(--cf-bili-blue-dark);
}
.input-bar button:disabled {
  background: var(--cf-hover);
  color: var(--cf-text-4);
  cursor: not-allowed;
}
.pulse {
  animation: chatpulse 1.2s infinite;
}
@keyframes chatpulse {
  0%, 100% {
    opacity: 1;
  }
  50% {
    opacity: 0.4;
  }
}
.flow-col {
  width: 420px;
  border-left: 1px solid var(--cf-border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}
.flow-header {
  display: flex;
  align-items: center;
  padding: 14px 16px;
  min-height: 52px;
  box-sizing: border-box;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--cf-text-3);
  border-bottom: 1px solid var(--cf-border);
}
.flow-body {
  flex: 1;
  min-height: 0;
}
</style>
