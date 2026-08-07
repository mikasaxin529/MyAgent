<script setup lang="ts">
// 评测页：postEval + 4 指标卡 + per_tag 条形图。
// 迁移自 DevPilot Eval.tsx（SVG 横向条形图改为 div 横向条，更简单响应式）。
import { computed, ref } from 'vue'
import { postEval, type EvalResult } from '../api'

const result = ref<EvalResult | null>(null)
const loading = ref(false)
const err = ref('')

async function run(): Promise<void> {
  loading.value = true
  err.value = ''
  try {
    result.value = await postEval()
  } catch (e) {
    err.value = String(e)
  } finally {
    loading.value = false
  }
}

const metrics = computed(() => {
  if (!result.value) return []
  const r = result.value
  return [
    { label: 'Accuracy', value: (r.accuracy * 100).toFixed(1) + '%', color: 'var(--cf-bili-blue)' },
    { label: 'Task Completion', value: (r.task_completion_rate * 100).toFixed(1) + '%', color: 'var(--cf-green)' },
    { label: 'Avg Latency', value: r.avg_latency_ms.toFixed(0) + 'ms', color: 'var(--cf-amber)' },
    { label: 'Avg Token Cost', value: r.avg_token_cost.toFixed(0), color: 'var(--cf-bili-pink)' },
  ]
})

const tags = computed(() => {
  if (!result.value?.per_tag) return []
  return Object.entries(result.value.per_tag)
    .map(([tag, v]) => ({ tag, ...v }))
    .sort((a, b) => b.accuracy - a.accuracy)
})
</script>

<template>
  <div class="eval-page">
    <header class="eval-header">
      <h1 class="grad-bili-text">评测</h1>
      <button class="run-btn" :disabled="loading" @click="run">
        {{ loading ? '运行中…' : '运行评测' }}
      </button>
    </header>

    <div v-if="err" class="err-box">评测失败：{{ err }}</div>

    <div v-if="result" class="eval-body">
      <div class="metric-grid">
        <div v-for="m in metrics" :key="m.label" class="metric-card">
          <div class="metric-label">{{ m.label }}</div>
          <div class="metric-value" :style="{ color: m.color }">{{ m.value }}</div>
        </div>
      </div>

      <div class="chart-section">
        <div class="panel-title">Per-Tag 准确率</div>
        <div class="bar-chart">
          <div v-for="t in tags" :key="t.tag" class="bar-row">
            <div class="bar-label">{{ t.tag.length > 18 ? t.tag.slice(0, 18) + '…' : t.tag }}</div>
            <div class="bar-track">
              <div class="bar-fill" :style="{ width: (t.accuracy * 100) + '%' }" />
            </div>
            <div class="bar-value">{{ (t.accuracy * 100).toFixed(1) }}% · n={{ t.count }}</div>
          </div>
          <div v-if="tags.length === 0" class="empty-mini">无 per-tag 数据</div>
        </div>
      </div>
    </div>
    <div v-else-if="!loading && !err" class="empty">
      点击「运行评测」查看模型在评测集上的表现
    </div>
  </div>
</template>

<style scoped>
.eval-page {
  height: 100%;
  overflow-y: auto;
  padding: 24px;
}
.eval-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 24px;
}
.eval-header h1 {
  font-size: 20px;
  margin: 0;
}
.run-btn {
  padding: 8px 20px;
  border-radius: 10px;
  background: var(--cf-bili-blue);
  color: #fff;
  border: none;
  cursor: pointer;
  font-size: 14px;
}
.run-btn:disabled {
  background: var(--cf-hover);
  color: var(--cf-text-4);
}
.err-box {
  padding: 12px 16px;
  border-radius: 10px;
  background: rgba(242, 93, 89, 0.1);
  border: 1px solid var(--cf-red);
  color: var(--cf-red);
  font-size: 13px;
  margin-bottom: 16px;
}
.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 32px;
}
.metric-card {
  padding: 16px 20px;
  border-radius: var(--cf-radius-md);
  background: var(--cf-card);
  border: 1px solid var(--cf-border);
}
.metric-label {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--cf-text-3);
}
.metric-value {
  font-size: 24px;
  font-weight: 700;
  margin-top: 6px;
}
.chart-section {
  background: var(--cf-card);
  border: 1px solid var(--cf-border);
  border-radius: var(--cf-radius-md);
  padding: 16px 20px;
}
.panel-title {
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--cf-text-3);
  margin-bottom: 16px;
}
.bar-chart {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.bar-row {
  display: flex;
  align-items: center;
  gap: 12px;
}
.bar-label {
  width: 140px;
  font-size: 12px;
  color: var(--cf-text-2);
  text-align: right;
  flex-shrink: 0;
}
.bar-track {
  flex: 1;
  height: 22px;
  background: var(--cf-hover);
  border-radius: 4px;
  overflow: hidden;
}
.bar-fill {
  height: 100%;
  background: var(--cf-bili-blue);
  border-radius: 4px;
  transition: width 0.3s;
}
.bar-value {
  width: 90px;
  font-size: 12px;
  color: var(--cf-text-3);
  flex-shrink: 0;
}
.empty-mini,
.empty {
  color: var(--cf-text-4);
  font-size: 14px;
  padding: 40px 0;
  text-align: center;
}
</style>
