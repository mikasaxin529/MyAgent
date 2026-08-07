<script setup lang="ts">
// 技能页：getSkills 手风琴列表。迁移自 DevPilot Skills.tsx。
import { onMounted, ref } from 'vue'
import { getSkills, type Skill } from '../api'

const skills = ref<Skill[]>([])
const open = ref<Record<string, boolean>>({})
const err = ref('')

onMounted(async () => {
  try {
    skills.value = await getSkills()
  } catch (e) {
    err.value = String(e)
  }
})

function toggle(name: string): void {
  open.value[name] = !open.value[name]
}
</script>

<template>
  <div class="skills-page">
    <header class="skills-header">
      <h1 class="grad-bili-text">技能</h1>
      <span class="sub">已注册的 AI Skill 清单</span>
    </header>

    <div v-if="err" class="err-box">加载失败：{{ err }}</div>

    <div class="skill-list">
      <div v-for="s in skills" :key="s.name" class="skill-card">
        <button class="skill-head" @click="toggle(s.name)">
          <span class="skill-icon">{{ s.name.slice(0, 2).toUpperCase() }}</span>
          <span class="skill-name">{{ s.name }}</span>
          <span class="spec-count">{{ s.specs.length }} specs</span>
          <span class="arrow" :class="{ open: open[s.name] }">▶</span>
        </button>
        <div v-if="open[s.name]" class="spec-list">
          <div v-for="(sp, i) in s.specs" :key="i" class="spec-card">
            <div class="spec-name">{{ sp.name }}</div>
            <div class="spec-desc">{{ sp.description }}</div>
            <div class="spec-schema-label">Schema</div>
            <pre class="spec-schema">{{ JSON.stringify(sp.schema, null, 2) }}</pre>
          </div>
        </div>
      </div>
      <div v-if="skills.length === 0 && !err" class="empty">无已注册技能</div>
    </div>
  </div>
</template>

<style scoped>
.skills-page {
  height: 100%;
  overflow-y: auto;
  padding: 24px;
}
.skills-header {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 24px;
}
.skills-header h1 {
  font-size: 20px;
  margin: 0;
}
.sub {
  font-size: 12px;
  color: var(--cf-text-3);
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
.skill-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.skill-card {
  border: 1px solid var(--cf-border);
  border-radius: var(--cf-radius-md);
  background: var(--cf-card);
  overflow: hidden;
}
.skill-head {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px 18px;
  background: none;
  border: none;
  cursor: pointer;
  color: var(--cf-text-1);
  font-size: 14px;
}
.skill-head:hover {
  background: var(--cf-hover);
}
.skill-icon {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: linear-gradient(135deg, var(--cf-bili-blue), var(--cf-bili-pink));
  color: #fff;
  font-size: 12px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.skill-name {
  font-weight: 600;
  font-family: ui-monospace, monospace;
  color: var(--cf-bili-blue);
}
.spec-count {
  font-size: 12px;
  color: var(--cf-text-3);
  margin-left: auto;
}
.arrow {
  font-size: 10px;
  color: var(--cf-text-3);
  transition: transform 0.15s;
}
.arrow.open {
  transform: rotate(90deg);
}
.spec-list {
  padding: 0 18px 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  border-top: 1px solid var(--cf-border);
}
.spec-card {
  padding: 12px 16px;
  border-radius: 8px;
  background: var(--cf-bg);
  border: 1px solid var(--cf-border-soft);
}
.spec-name {
  font-family: ui-monospace, monospace;
  font-size: 13px;
  color: var(--cf-bili-blue);
  font-weight: 600;
}
.spec-desc {
  font-size: 12px;
  color: var(--cf-text-3);
  margin: 4px 0 8px;
}
.spec-schema-label {
  font-size: 10px;
  text-transform: uppercase;
  color: var(--cf-text-4);
  margin-bottom: 4px;
}
.spec-schema {
  margin: 0;
  padding: 8px 12px;
  border-radius: 6px;
  background: var(--cf-sidebar);
  font-size: 11px;
  font-family: ui-monospace, monospace;
  color: var(--cf-text-2);
  overflow-x: auto;
}
.empty {
  color: var(--cf-text-4);
  font-size: 14px;
  padding: 40px 0;
  text-align: center;
}
</style>
