<script setup lang="ts">
// dev 全链路 + 审批握手页。迁移自 DevPilot Run.tsx（状态机 + RunSocket + 三态审批）。
import { computed, onUnmounted, ref } from 'vue'
import { Play } from 'lucide-vue-next'
import { RunSocket, type AuditEntry, type BlackboardData, type DoneSummary, type ServerFrame } from '../api'

type Status = 'idle' | 'connecting' | 'running' | 'awaiting_approval' | 'done' | 'error'

const task = ref('')
const status = ref<Status>('idle')
const audit = ref<AuditEntry[]>([])
const blackboard = ref<BlackboardData | null>(null)
const approval = ref<{ action: string; args: Record<string, unknown>; reason: string } | null>(null)
const summary = ref<DoneSummary | null>(null)
const decisionComment = ref('')
const editArgs = ref('')
let socket: RunSocket | null = null

const EVENT_COLORS: Record<string, string> = {
  agent_step: '#6366f1',
  llm_call: '#a855f7',
  tool_call: '#10b981',
  approval: '#f5a623',
}

const STATUS_STYLES: Record<Status, { color: string; label: string }> = {
  idle: { color: 'var(--cf-text-3)', label: 'idle' },
  connecting: { color: 'var(--cf-amber)', label: 'connecting' },
  running: { color: 'var(--cf-bili-blue)', label: 'running' },
  awaiting_approval: { color: 'var(--cf-amber)', label: 'awaiting approval' },
  done: { color: 'var(--cf-green)', label: 'done' },
  error: { color: 'var(--cf-red)', label: 'error' },
}

const statusBadge = computed(() => STATUS_STYLES[status.value])
const canStart = computed(() => status.value === 'idle' || status.value === 'done' || status.value === 'error')

function onStart(): void {
  const t = task.value.trim()
  if (!t || !canStart.value) return
  audit.value = []
  blackboard.value = null
  approval.value = null
  summary.value = null
  status.value = 'connecting'
  const sock = new RunSocket({
    onOpen: () => {
      sock.send({ task: t })
      status.value = 'running'
    },
    onFrame: (f: ServerFrame) => handleFrame(f),
    onClose: () => {
      if (status.value === 'running') status.value = 'done'
    },
    onError: () => {
      status.value = 'error'
    },
  })
  socket = sock
  sock.connect()
}

function handleFrame(frame: ServerFrame): void {
  switch (frame.type) {
    case 'audit':
      audit.value.push(frame.entry)
      break
    case 'blackboard':
      blackboard.value = frame.data
      break
    case 'approval_request':
      approval.value = { action: frame.action, args: frame.args, reason: frame.reason }
      editArgs.value = JSON.stringify(frame.args, null, 2)
      decisionComment.value = ''
      status.value = 'awaiting_approval'
      break
    case 'done':
      blackboard.value = frame.blackboard
      summary.value = frame.summary
      approval.value = null
      status.value = 'done'
      socket?.close()
      break
  }
}

function sendDecision(decision: 'approve' | 'reject' | 'edit'): void {
  if (!approval.value || !socket) return
  let args = approval.value.args
  if (decision === 'edit') {
    try {
      args = JSON.parse(editArgs.value)
    } catch {
      alert('Edit args 不是合法 JSON')
      return
    }
  }
  socket.send({ decision, comment: decisionComment.value, args })
  approval.value = null
  status.value = 'running'
}

onUnmounted(() => socket?.close())
</script>

<template>
  <div class="run-page">
    <header class="run-header">
      <h1 class="grad-bili-text">编排运行</h1>
      <span class="status-badge" :style="{ color: statusBadge.color, borderColor: statusBadge.color }">
        {{ statusBadge.label }}
      </span>
    </header>

    <div class="run-form">
      <textarea
        v-model="task"
        placeholder="输入开发任务，如「新增一个计算器工具函数」"
        rows="2"
      />
      <button class="start-btn" :disabled="!canStart || !task.trim()" @click="onStart">
        <Play :size="15" /> 开始
      </button>
    </div>

    <div class="run-body">
      <!-- 左：Timeline -->
      <div class="timeline-col">
        <div class="panel-title">审计时间线</div>
        <div class="timeline">
          <div v-for="(e, i) in audit" :key="i" class="tl-item">
            <span class="tl-event" :style="{ color: EVENT_COLORS[e.event] || 'var(--cf-text-3)' }">● {{ e.event }}</span>
            <span class="tl-actor">{{ e.actor }}</span>
            <span class="tl-detail">{{ JSON.stringify(e.detail) }}</span>
            <span class="tl-meta">{{ e.timestamp }} · {{ e.trace_id }}</span>
          </div>
          <div v-if="audit.length === 0" class="empty-mini">无审计事件</div>
        </div>
      </div>

      <!-- 右：Blackboard -->
      <div class="bb-col">
        <div class="panel-title">黑板</div>
        <div class="bb-grid">
          <div class="bb-panel">
            <div class="bb-label">Plan</div>
            <pre class="bb-pre">{{ (blackboard?.plan ?? []).join('\n') }}</pre>
          </div>
          <div class="bb-panel">
            <div class="bb-label">Code Diff</div>
            <pre class="bb-pre emerald">{{ blackboard?.code_diff || '—' }}</pre>
          </div>
          <div class="bb-panel">
            <div class="bb-label">Review</div>
            <pre class="bb-pre">{{ blackboard?.review || '—' }}</pre>
          </div>
          <div class="bb-panel">
            <div class="bb-label">Test Result</div>
            <pre class="bb-pre">{{ blackboard?.test_result || '—' }}</pre>
          </div>
        </div>
        <div v-if="summary" class="bb-summary">
          <span class="bb-sum-label">总事件 {{ summary.total_events }}</span>
          <span v-for="(c, k) in summary.by_event" :key="k" class="bb-tag">{{ k }}: {{ c }}</span>
        </div>
      </div>
    </div>

    <!-- 审批模态 -->
    <div v-if="approval" class="modal-mask">
      <div class="modal-card">
        <h2 class="modal-title">审批请求</h2>
        <div class="modal-row"><span class="mr-label">Action</span><span class="mr-value">{{ approval.action }}</span></div>
        <div class="modal-row"><span class="mr-label">Reason</span><span class="mr-value">{{ approval.reason }}</span></div>
        <div class="modal-args">
          <div class="mr-label">Args（可编辑）</div>
          <textarea v-model="editArgs" rows="8" class="args-input" />
        </div>
        <input v-model="decisionComment" placeholder="Comment（可选）" class="comment-input" />
        <div class="modal-actions">
          <button class="btn-reject" @click="sendDecision('reject')">Reject</button>
          <button class="btn-edit" @click="sendDecision('edit')">Edit</button>
          <button class="btn-approve" @click="sendDecision('approve')">Approve</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.run-page {
  height: 100%;
  display: flex;
  flex-direction: column;
}
.run-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 24px;
  border-bottom: 1px solid var(--cf-border);
}
.run-header h1 {
  font-size: 18px;
  margin: 0;
}
.status-badge {
  font-size: 12px;
  padding: 2px 10px;
  border: 1px solid;
  border-radius: 99px;
}
.run-form {
  display: flex;
  gap: 12px;
  padding: 16px 24px;
  border-bottom: 1px solid var(--cf-border);
}
.run-form textarea {
  flex: 1;
  padding: 10px 14px;
  border-radius: 10px;
  background: var(--cf-card);
  border: 1px solid var(--cf-border);
  color: var(--cf-text-1);
  font-size: 14px;
  font-family: inherit;
  resize: none;
}
.start-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 20px;
  border-radius: 10px;
  background: var(--cf-bili-blue);
  color: #fff;
  border: none;
  cursor: pointer;
  font-size: 14px;
}
.start-btn:disabled {
  background: var(--cf-hover);
  color: var(--cf-text-4);
  cursor: not-allowed;
}
.run-body {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1px;
  background: var(--cf-border);
  min-height: 0;
}
.timeline-col,
.bb-col {
  background: var(--cf-bg);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.panel-title {
  padding: 10px 16px;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--cf-text-3);
  border-bottom: 1px solid var(--cf-border);
}
.timeline {
  flex: 1;
  overflow-y: auto;
  padding: 12px 16px;
}
.tl-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 0;
  border-bottom: 1px solid var(--cf-border-soft);
}
.tl-event {
  font-size: 13px;
  font-weight: 600;
}
.tl-actor {
  font-size: 12px;
  color: var(--cf-text-2);
}
.tl-detail {
  font-size: 11px;
  font-family: ui-monospace, monospace;
  color: var(--cf-text-3);
  word-break: break-all;
}
.tl-meta {
  font-size: 10px;
  color: var(--cf-text-4);
}
.empty-mini {
  color: var(--cf-text-4);
  font-size: 13px;
  padding: 16px 0;
}
.bb-grid {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 1fr;
  grid-template-rows: 1fr 1fr;
  gap: 1px;
  background: var(--cf-border);
  min-height: 0;
}
.bb-panel {
  background: var(--cf-bg);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.bb-label {
  padding: 8px 12px;
  font-size: 11px;
  color: var(--cf-text-3);
  border-bottom: 1px solid var(--cf-border);
}
.bb-pre {
  flex: 1;
  margin: 0;
  padding: 10px 12px;
  overflow: auto;
  font-size: 12px;
  font-family: ui-monospace, monospace;
  color: var(--cf-text-2);
  white-space: pre-wrap;
}
.bb-pre.emerald {
  color: var(--cf-green);
}
.bb-summary {
  padding: 10px 12px;
  border-top: 1px solid var(--cf-border);
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.bb-sum-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--cf-text-1);
}
.bb-tag {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 99px;
  background: var(--cf-hover);
  color: var(--cf-text-2);
}
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 50;
}
.modal-card {
  width: 560px;
  max-height: 80vh;
  overflow: auto;
  background: var(--cf-card);
  border: 1px solid var(--cf-border);
  border-radius: var(--cf-radius-lg);
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.modal-title {
  font-size: 18px;
  margin: 0;
}
.modal-row {
  display: flex;
  gap: 8px;
  font-size: 13px;
}
.mr-label {
  color: var(--cf-text-3);
  min-width: 60px;
}
.mr-value {
  color: var(--cf-text-1);
  word-break: break-all;
}
.modal-args {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.args-input {
  padding: 10px;
  border-radius: 8px;
  background: var(--cf-bg);
  border: 1px solid var(--cf-border);
  color: var(--cf-text-1);
  font-family: ui-monospace, monospace;
  font-size: 12px;
  resize: vertical;
}
.comment-input {
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--cf-bg);
  border: 1px solid var(--cf-border);
  color: var(--cf-text-1);
  font-size: 13px;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
.modal-actions button {
  padding: 8px 18px;
  border-radius: 8px;
  font-size: 13px;
  cursor: pointer;
  border: 1px solid;
}
.btn-reject {
  background: transparent;
  border-color: var(--cf-red);
  color: var(--cf-red);
}
.btn-edit {
  background: transparent;
  border-color: var(--cf-bili-blue);
  color: var(--cf-bili-blue);
}
.btn-approve {
  background: var(--cf-green);
  border-color: var(--cf-green);
  color: #fff;
}
</style>
