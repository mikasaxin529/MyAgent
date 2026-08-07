<script setup lang="ts">
// 单条消息气泡：ChatFlow 式——头像 + think-block(分组折叠) + tool-block-sources(搜索卡片) +
// ai-content(markdown) + step-done。markdown 管线：marked + katex + hljs（照 ChatFlow MessageItem.vue）。
import { ref, reactive, watch, nextTick, onMounted, onUpdated, computed } from 'vue'
import { marked } from 'marked'
import markedKatex from 'marked-katex-extension'
import hljs from 'highlight.js/lib/common'
import 'katex/dist/katex.min.css'
import { Brain, ChevronRight, Copy, Check, Search, Cloud, FileText } from 'lucide-vue-next'
import { NODE_LABEL, type Message, type ThinkingSegment } from '../types'

const props = defineProps<{ message: Message }>()

marked.use(markedKatex({ throwOnError: false }))

const html = ref('')
const rootRef = ref<HTMLElement | null>(null)
const copiedMsg = ref(false)
const thinkOpen = reactive<Record<string, boolean>>({})

function render(): void {
  // chat 路由（steps 空）直接渲染 m.content；多步任务的正文在 step.content 内各自渲染。
  const c = props.message.content
  if (!c) {
    html.value = ''
    return
  }
  html.value = marked.parse(c, { async: false }) as string
}

function renderMd(text: string): string {
  if (!text) return ''
  return marked.parse(text, { async: false }) as string
}

/** done 后增强代码块：加语言标签 + 复制按钮 + hljs 高亮。 */
function enhance(): void {
  const root = rootRef.value
  if (!root) return
  root.querySelectorAll('pre').forEach((pre) => {
    if (pre.parentElement?.classList.contains('code-block')) return
    const code = pre.querySelector('code')
    if (!code) return
    const cls = code.className || ''
    const m = cls.match(/language-(\w+)/)
    const lang = m ? m[1] : 'text'
    const text = code.textContent || ''
    try {
      if (hljs.getLanguage(lang)) {
        code.innerHTML = hljs.highlight(text, { language: lang }).value
      } else {
        code.innerHTML = hljs.highlightAuto(text).value
      }
      code.classList.add('hljs')
    } catch {
      /* ignore */
    }
    const wrap = document.createElement('div')
    wrap.className = 'code-block'
    const head = document.createElement('div')
    head.className = 'cb-head'
    const langSpan = document.createElement('span')
    langSpan.className = 'cb-lang'
    langSpan.textContent = lang
    const btn = document.createElement('button')
    btn.className = 'cb-copy'
    btn.dataset.code = encodeURIComponent(text)
    btn.textContent = '复制'
    head.appendChild(langSpan)
    head.appendChild(btn)
    pre.parentNode?.insertBefore(wrap, pre)
    wrap.appendChild(head)
    wrap.appendChild(pre)
  })
}

watch(() => props.message.content, () => { render(); if (props.message.done) nextTick(enhance) }, { immediate: true })
watch(() => props.message.done, (d) => d && nextTick(enhance))
onMounted(() => { if (props.message.done) nextTick(enhance) })
onUpdated(() => { if (props.message.done) nextTick(enhance) })

function onCodeClick(e: MouseEvent): void {
  const target = (e.target as HTMLElement).closest('.cb-copy') as HTMLElement | null
  if (!target) return
  const text = decodeURIComponent(target.dataset.code || '')
  navigator.clipboard.writeText(text).then(() => {
    target.textContent = '已复制'
    target.classList.add('cb-done')
    setTimeout(() => { target.textContent = '复制'; target.classList.remove('cb-done') }, 1500)
  })
}

function copyMessage(): void {
  const text = props.message.content || props.message.steps.map((s) => s.content).join('\n\n')
  navigator.clipboard.writeText(text).then(() => {
    copiedMsg.value = true
    setTimeout(() => (copiedMsg.value = false), 1500)
  })
}

/** 把同 (node, stepIndex) 的 reasoning + content segment 合并成一个 group。 */
interface ThinkGroup { node: string; stepIndex: number | null; reasoning: string; content: string }
function groupSegments(segs: ThinkingSegment[]): ThinkGroup[] {
  const groups: ThinkGroup[] = []
  for (const s of segs) {
    let g = groups.find((x) => x.node === s.node && x.stepIndex === s.stepIndex)
    if (!g) {
      g = { node: s.node, stepIndex: s.stepIndex, reasoning: '', content: '' }
      groups.push(g)
    }
    if (s.phase === 'reasoning') g.reasoning += s.content
    else g.content += s.content
  }
  return groups
}

const msgLevelGroups = computed(() => groupSegments(props.message.thinkingSegments))

function toolLabel(name: string): string {
  if (name === 'websearch') return '搜索了网络'
  if (name === 'weather_current' || name === 'weather_forecast') return '查询天气'
  if (name === 'websearch_fetch_page') return '抓取网页'
  return name
}
function toolIcon(name: string) {
  if (name === 'websearch' || name === 'websearch_fetch_page') return Search
  if (name.startsWith('weather')) return Cloud
  return FileText
}
function favicon(url: string): string {
  try {
    const u = new URL(url)
    return `https://www.google.com/s2/favicons?domain=${u.hostname}&sz=32`
  } catch {
    return ''
  }
}
function statusText(s: string): string {
  return s === 'running' ? '进行中' : s === 'done' ? '已完成' : '待执行'
}
</script>

<template>
  <div class="msg-row" :class="message.role === 'user' ? 'is-user' : 'is-bot'">
    <div class="avatar" :class="message.role === 'user' ? 'avatar-user' : 'avatar-bot'">
      <span v-if="message.role === 'user'">我</span>
      <Brain v-else :size="16" />
    </div>
    <div class="bubble-col" :class="message.role === 'user' ? 'col-user' : 'col-bot'">
      <template v-if="message.role === 'user'">
        <div class="bubble bubble-user">{{ message.content }}</div>
      </template>
      <template v-else>
        <!-- 消息级 think-block（route/planner/reflector，step_index=null） -->
        <div v-for="(g, i) in msgLevelGroups" :key="'mt'+i" class="think-block">
          <button class="think-toggle" @click="thinkOpen['m'+i] = !thinkOpen['m'+i]">
            <ChevronRight :size="13" :class="{ rotated: thinkOpen['m'+i] }" class="chev" />
            <Brain :size="13" />
            <span>{{ NODE_LABEL[g.node] || '思考' }}</span>
          </button>
          <div v-if="thinkOpen['m'+i]" class="think-body">
            <div v-if="g.reasoning" class="think-phase think-phase-reasoning">{{ g.reasoning }}</div>
            <div v-if="g.content" class="think-phase think-phase-content">{{ g.content }}</div>
          </div>
        </div>

        <!-- 步骤 -->
        <div v-for="(step, si) in message.steps" :key="'step'+si" class="step-section" :class="'step-status-' + step.status">
          <div class="step-header">
            <span class="step-idx">步骤{{ step.index + 1 }}</span>
            <span class="step-title">{{ step.title }}</span>
            <span class="step-badge" :class="step.status">{{ statusText(step.status) }}</span>
          </div>
          <!-- 步骤级 think-block -->
          <div v-for="(g, ti) in groupSegments(step.thinkingSegments)" :key="'st'+si+ti" class="think-block">
            <button class="think-toggle" @click="thinkOpen['s'+si+ti] = !thinkOpen['s'+si+ti]">
              <ChevronRight :size="13" :class="{ rotated: thinkOpen['s'+si+ti] }" class="chev" />
              <Brain :size="13" />
              <span>{{ NODE_LABEL[g.node] || '思考' }}</span>
            </button>
            <div v-if="thinkOpen['s'+si+ti]" class="think-body">
              <div v-if="g.reasoning" class="think-phase think-phase-reasoning">{{ g.reasoning }}</div>
              <div v-if="g.content" class="think-phase think-phase-content">{{ g.content }}</div>
            </div>
          </div>
          <!-- 工具调用卡片 -->
          <div v-for="(tc, ti) in step.toolCalls" :key="'tc'+si+ti" class="tool-block" :class="'tool-mode-' + tc.displayMode">
            <div class="tool-label">
              <component :is="toolIcon(tc.name)" :size="13" class="tool-ic" />
              <span>{{ toolLabel(tc.name) }}</span>
            </div>
            <div v-if="tc.input.query" class="tool-query">{{ tc.input.query }}</div>
            <div v-if="tc.searchItems.length" class="search-url-list">
              <a v-for="(item, ii) in tc.searchItems" :key="ii" :href="item.url" target="_blank" rel="noopener" class="search-url-row">
                <img v-if="favicon(item.url)" :src="favicon(item.url)" class="favicon" alt="" loading="lazy" />
                <span class="su-title">{{ item.title || item.url }}</span>
                <span class="su-url">{{ item.url }}</span>
              </a>
            </div>
            <pre v-else-if="tc.output" class="tool-output">{{ tc.output }}</pre>
          </div>
          <!-- 步骤正文（markdown） -->
          <div v-if="step.content" class="ai-content md-body" v-html="renderMd(step.content)" @click="onCodeClick" />
        </div>

        <!-- 主正文气泡（chat 路由 steps 空，直接 m.content） -->
        <div v-if="message.content && message.steps.length === 0" class="bubble bubble-bot" ref="rootRef">
          <div class="md-body" v-html="html" @click="onCodeClick" />
          <button class="copy-msg" @click="copyMessage">
            <Check v-if="copiedMsg" :size="12" />
            <Copy v-else :size="12" />
            {{ copiedMsg ? '已复制' : '复制' }}
          </button>
        </div>
        <span v-else-if="message.error" class="error-text">{{ message.error }}</span>
        <span v-else-if="!message.steps.length && !message.content" class="typing">
          <Brain :size="13" class="pulse" /> 生成中…
        </span>
        <button v-if="message.role === 'assistant' && (message.content || message.steps.length)" class="copy-msg copy-msg-abs" @click="copyMessage">
          <Check v-if="copiedMsg" :size="12" />
          <Copy v-else :size="12" />
          {{ copiedMsg ? '已复制' : '复制' }}
        </button>
      </template>
    </div>
  </div>
</template>

<style scoped>
.msg-row { display: flex; gap: 12px; width: 100%; }
.msg-row.is-user { flex-direction: row-reverse; }
.avatar { width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center; flex-shrink: 0; font-size: 13px; color: #fff; }
.avatar-user { background: var(--cf-bili-blue); }
.avatar-bot { background: linear-gradient(135deg, var(--cf-bili-blue), var(--cf-bili-pink)); }
.bubble-col { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.col-user { align-items: flex-end; max-width: 75%; }
.col-bot { align-items: flex-start; flex: 1; max-width: 88%; }

/* think-block */
.think-block { width: 100%; }
.think-toggle { display: flex; align-items: center; gap: 6px; background: none; border: none; color: var(--cf-text-3); font-size: 12px; cursor: pointer; padding: 2px 0; }
.think-toggle:hover { color: var(--cf-text-1); }
.chev { transition: transform 0.15s; }
.chev.rotated { transform: rotate(90deg); }
.think-body { margin-top: 4px; padding: 8px 12px; border-radius: var(--cf-radius-sm); background: rgba(28,31,36,0.6); border: 1px solid var(--cf-border); }
.think-phase { white-space: pre-wrap; font-size: 12px; line-height: 1.6; }
.think-phase-reasoning { color: var(--cf-text-4); font-style: italic; }
.think-phase-content { color: var(--cf-text-3); margin-top: 4px; }

/* step */
.step-section { width: 100%; padding: 8px 0; border-left: 2px solid var(--cf-border); padding-left: 12px; margin: 4px 0; }
.step-section.step-status-running { border-left-color: var(--cf-amber); }
.step-section.step-status-done { border-left-color: var(--cf-green); }
.step-header { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--cf-text-3); margin-bottom: 6px; }
.step-idx { color: var(--cf-bili-blue); font-weight: 600; }
.step-title { color: var(--cf-text-2); }
.step-badge { font-size: 10px; padding: 1px 6px; border-radius: 8px; background: var(--cf-hover); color: var(--cf-text-4); }
.step-badge.running { background: rgba(245,166,35,0.15); color: var(--cf-amber); }
.step-badge.done { background: rgba(0,181,120,0.15); color: var(--cf-green); }

/* tool-block */
.tool-block { margin: 6px 0; padding: 8px 12px; border-radius: var(--cf-radius-sm); background: rgba(28,31,36,0.5); border: 1px solid var(--cf-border); }
.tool-label { display: flex; align-items: center; gap: 5px; font-size: 12px; color: var(--cf-text-3); font-weight: 600; }
.tool-ic { color: var(--cf-bili-blue); }
.tool-query { font-size: 12px; color: var(--cf-text-2); margin: 4px 0; font-style: italic; }
.search-url-list { display: flex; flex-direction: column; gap: 4px; margin-top: 6px; }
.search-url-row { display: flex; align-items: center; gap: 8px; padding: 5px 8px; border-radius: 6px; background: var(--cf-hover); text-decoration: none; transition: background 0.15s; }
.search-url-row:hover { background: var(--cf-active); }
.favicon { width: 14px; height: 14px; border-radius: 3px; flex-shrink: 0; }
.su-title { font-size: 12px; color: var(--cf-text-2); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.su-url { font-size: 10px; color: var(--cf-text-4); font-family: ui-monospace, monospace; }
.tool-output { margin: 4px 0 0; padding: 8px; font-size: 12px; color: var(--cf-text-3); background: #0d1117; border-radius: 6px; overflow-x: auto; white-space: pre-wrap; }

/* ai-content / 主气泡 */
.bubble { position: relative; padding: 10px 16px; border-radius: 16px; font-size: 14px; line-height: 1.7; }
.bubble-user { background: var(--cf-bili-blue); color: #fff; border-radius: 16px 16px 4px 16px; white-space: pre-wrap; }
.bubble-bot { background: var(--cf-card); border: 1px solid var(--cf-border); border-radius: 16px 16px 16px 4px; max-width: 100%; }
.ai-content { padding: 8px 12px; border-radius: var(--cf-radius-sm); background: var(--cf-card); border: 1px solid var(--cf-border); font-size: 14px; line-height: 1.7; }

.md-body { max-width: 100%; word-break: break-word; }
.md-body :deep(p) { margin: 8px 0; }
.md-body :deep(p:first-child) { margin-top: 0; }
.md-body :deep(p:last-child) { margin-bottom: 0; }
.md-body :deep(h1) { font-size: 18px; font-weight: 600; margin: 16px 0 8px; color: var(--cf-text-1); }
.md-body :deep(h2) { font-size: 16px; font-weight: 600; margin: 12px 0 8px; color: var(--cf-text-1); }
.md-body :deep(h3) { font-size: 14px; font-weight: 600; margin: 12px 0 4px; color: var(--cf-text-1); }
.md-body :deep(ul) { list-style: disc outside; margin: 8px 0; padding-left: 20px; }
.md-body :deep(ol) { list-style: decimal outside; margin: 8px 0; padding-left: 20px; }
.md-body :deep(li) { font-size: 14px; margin: 4px 0; }
.md-body :deep(a) { color: var(--cf-bili-blue); text-decoration: underline; text-underline-offset: 2px; }
.md-body :deep(blockquote) { border-left: 3px solid var(--cf-bili-blue); padding-left: 12px; margin: 8px 0; color: var(--cf-text-3); font-style: italic; }
.md-body :deep(table) { border-collapse: collapse; margin: 12px 0; display: block; overflow-x: auto; max-width: 100%; }
.md-body :deep(th) { padding: 8px 12px; background: var(--cf-hover); text-align: left; font-weight: 600; font-size: 12px; border: 1px solid var(--cf-border); }
.md-body :deep(td) { padding: 8px 12px; border: 1px solid var(--cf-border); font-size: 13px; }
.md-body :deep(hr) { border: none; border-top: 1px solid var(--cf-border); margin: 16px 0; }
.md-body :deep(code) { font-family: ui-monospace, monospace; }
.md-body :deep(.code-block) { margin: 12px 0; border-radius: 8px; overflow: hidden; border: 1px solid var(--cf-border); background: #0d1117; }
.md-body :deep(.cb-head) { display: flex; justify-content: space-between; align-items: center; padding: 6px 12px; background: rgba(35,38,44,0.6); border-bottom: 1px solid var(--cf-border); }
.md-body :deep(.cb-lang) { font-size: 11px; color: var(--cf-text-3); font-family: ui-monospace, monospace; }
.md-body :deep(.cb-copy) { background: none; border: none; color: var(--cf-text-3); font-size: 11px; cursor: pointer; }
.md-body :deep(.cb-copy:hover) { color: var(--cf-text-1); }
.md-body :deep(.cb-copy.cb-done) { color: var(--cf-green); }
.md-body :deep(pre) { margin: 0; padding: 12px 16px; overflow-x: auto; font-size: 13px; line-height: 1.6; background: #0d1117; border-radius: 8px; }

.error-text { color: var(--cf-red); }
.typing { display: inline-flex; align-items: center; gap: 6px; color: var(--cf-text-3); font-style: italic; }
.pulse { animation: msgpulse 1.2s infinite; }
@keyframes msgpulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
.copy-msg { display: flex; align-items: center; gap: 4px; padding: 3px 8px; border-radius: 6px; background: var(--cf-hover); border: 1px solid var(--cf-border); color: var(--cf-text-3); font-size: 11px; cursor: pointer; opacity: 0; transition: opacity 0.15s; align-self: flex-end; }
.copy-msg-abs { position: absolute; bottom: -10px; right: 8px; }
.bubble-bot:hover .copy-msg { opacity: 1; }
</style>
