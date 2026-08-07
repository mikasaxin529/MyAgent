<script setup lang="ts">
// 动态图形流：收到 plan 帧的步骤列表后动态建图 [planner, step1, step2, ...]，
// dagre 布局，边连顺序链 planner→step1→step2→...。node 帧按 node_id 更新状态色。
// 迁移自 DevPilot web/frontend/src/components/FlowGraph.tsx（@xyflow/react → @vue-flow/core）。
import { ref, watch, nextTick } from 'vue'
import { VueFlow, Handle, Position, useVueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import dagre from 'dagre'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'
import '@vue-flow/minimap/dist/style.css'
import type { PlanStep, NodeUpdate, NodeStatus } from '../api'

interface DevNodeData {
  label: string
  status: NodeStatus
  kind: 'planner' | 'step'
  needsSearch?: boolean
}

interface FlowNode {
  id: string
  type: string
  position: { x: number; y: number }
  data: DevNodeData
}

interface FlowEdge {
  id: string
  source: string
  target: string
}

const props = defineProps<{
  plan?: PlanStep[] | null
  nodeUpdates?: NodeUpdate[]
}>()

const nodes = ref<FlowNode[]>([])
const edges = ref<FlowEdge[]>([])

const { onInit, fitView } = useVueFlow()

// 全图适配视口：plan 变化重建图后，让 vue-flow 自己把所有节点缩放到可见范围，
// 保证整条链不拖动也能看全。planner 由 dagre TB 布局自然落在最上方。
// 加 requestAnimationFrame 确保节点 DOM 渲染完再 fit（vue-flow 异步处理 prop→内部 nodes）。
function fitGraph() {
  fitView({ padding: 0.25, maxZoom: 1.0 })
}

onInit(() => {
  nextTick(() => requestAnimationFrame(fitGraph))
})

function statusStyle(status: NodeStatus) {
  switch (status) {
    case 'running':
      return { border: '#f59e0b', dot: '#fbbf24', text: '#fcd34d' }
    case 'done':
      return { border: '#10b981', dot: '#34d399', text: '#6ee7b7' }
    case 'error':
      return { border: '#f43f5e', dot: '#fb7185', text: '#fda4af' }
    default:
      return { border: '#334155', dot: '#475569', text: '#64748b' }
  }
}

function getLayouted(ns: FlowNode[], es: FlowEdge[], direction: 'LR' | 'TB' = 'TB') {
  const g = new dagre.graphlib.Graph()
  g.setDefaultEdgeLabel(() => ({}))
  g.setGraph({ rankdir: direction, nodesep: 40, ranksep: 90 })
  const w = 150
  const h = 52
  ns.forEach((n) => g.setNode(n.id, { width: w, height: h }))
  es.forEach((e) => g.setEdge(e.source, e.target))
  dagre.layout(g)
  return {
    nodes: ns.map((n) => {
      const pos = g.node(n.id)
      return { ...n, position: { x: pos.x - w / 2, y: pos.y - h / 2 } }
    }),
    edges: es,
  }
}

function buildGraph(plan: PlanStep[] | null | undefined) {
  if (!plan || plan.length === 0) {
    const single: FlowNode = {
      id: 'planner',
      type: 'devNode',
      data: { label: 'Planner', status: 'idle', kind: 'planner' },
      position: { x: 0, y: 0 },
    }
    const r = getLayouted([single], [])
    nodes.value = r.nodes
    edges.value = r.edges
    return
  }
  const plannerNode: FlowNode = {
    id: 'planner',
    type: 'devNode',
    data: { label: 'Planner', status: 'idle', kind: 'planner' },
    position: { x: 0, y: 0 },
  }
  const stepNodes: FlowNode[] = plan.map((s) => ({
    id: s.id,
    type: 'devNode',
    data: { label: s.name, status: 'idle', kind: 'step', needsSearch: s.needs_search },
    position: { x: 0, y: 0 },
  }))
  const es: FlowEdge[] = []
  es.push({ id: 'planner-step1', source: 'planner', target: plan[0].id })
  for (let i = 0; i < plan.length - 1; i++) {
    es.push({ id: `${plan[i].id}-${plan[i + 1].id}`, source: plan[i].id, target: plan[i + 1].id })
  }
  const r = getLayouted([plannerNode, ...stepNodes], es)
  nodes.value = r.nodes
  edges.value = r.edges
}

watch(
  () => props.plan,
  (p) => {
    buildGraph(p)
    nextTick(() => requestAnimationFrame(fitGraph))
  },
  { immediate: true },
)

watch(
  () => props.nodeUpdates,
  (ups) => {
    if (!ups || ups.length === 0) return
    // 每个 node 取最新 update（反向查找，与原 React 版一致）。
    const latest = new Map<string, NodeStatus>()
    for (const u of ups) latest.set(u.node_id, u.status)
    nodes.value = nodes.value.map((n) => {
      const st = latest.get(n.id)
      if (!st) return n
      return { ...n, data: { ...n.data, status: st } }
    })
  },
  { deep: true },
)

function miniMapColor(n: { data?: DevNodeData }): string {
  const d = n.data
  if (!d) return '#334155'
  if (d.status === 'running') return '#f59e0b'
  if (d.status === 'done') return '#10b981'
  if (d.status === 'error') return '#f43f5e'
  return '#334155'
}
</script>

<template>
  <div class="flow-graph">
    <VueFlow
      :nodes="nodes"
      :edges="edges"
      :nodes-draggable="false"
      :nodes-connectable="false"
      :elements-selectable="false"
      :default-edge-options="{ type: 'smoothstep' }"
    >
      <Background :gap="16" :size="1" pattern-color="#1e293b" />
      <Controls :show-interactive="false" />
      <MiniMap pannable zoomable :node-color="miniMapColor" mask-color="rgba(2, 6, 23, 0.7)" />
      <template #node-devNode="{ data }">
        <div
          class="dev-node"
          :style="{ borderColor: statusStyle(data.status).border }"
          :class="{ pulse: data.status === 'running' }"
        >
          <Handle type="target" :position="Position.Top" :style="{ background: '#475569', width: '6px', height: '6px', border: 'none' }" />
          <div class="dev-row">
            <span class="dot" :style="{ background: statusStyle(data.status).dot }" />
            <span class="label" :style="{ color: statusStyle(data.status).text }">{{ data.label }}</span>
            <span v-if="data.needsSearch" class="badge" title="联网搜索">🌐</span>
            <span v-if="data.kind === 'planner'" class="badge planner" title="规划">⛳</span>
          </div>
          <Handle type="source" :position="Position.Bottom" :style="{ background: '#475569', width: '6px', height: '6px', border: 'none' }" />
        </div>
      </template>
    </VueFlow>
  </div>
</template>

<style scoped>
.flow-graph {
  height: 100%;
  background: #0f1115;
}
.dev-node {
  min-width: 140px;
  border: 2px solid #334155;
  background: #1c1f24;
  border-radius: 10px;
  padding: 8px 12px;
}
.dev-node.pulse {
  animation: devpulse 1.5s infinite;
}
@keyframes devpulse {
  0%,
  100% {
    box-shadow: 0 0 0 0 rgba(245, 158, 11, 0.4);
  }
  50% {
    box-shadow: 0 0 0 6px rgba(245, 158, 11, 0);
  }
}
.dev-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.label {
  font-size: 12px;
  font-weight: 500;
}
.badge {
  margin-left: auto;
  font-size: 11px;
}
.badge.planner {
  color: #a78bfa;
}
</style>
