<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'

const navs = [
  { to: '/', label: '聊天', icon: '💬' },
  { to: '/run', label: '编排', icon: '⚙️' },
  { to: '/eval', label: '评测', icon: '📊' },
  { to: '/skills', label: '技能', icon: '🧩' },
]
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand">DevPilot</div>
      <nav>
        <RouterLink
          v-for="n in navs"
          :key="n.to"
          :to="n.to"
          class="nav-item"
          active-class="active"
        >
          <span class="nav-icon">{{ n.icon }}</span>
          <span>{{ n.label }}</span>
        </RouterLink>
      </nav>
    </aside>
    <main class="main"><RouterView /></main>
  </div>
</template>

<style scoped>
.sidebar {
  width: 220px;
  background: var(--cf-sidebar);
  border-right: 1px solid var(--cf-border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}
.brand {
  padding: 20px 18px;
  font-weight: 700;
  font-size: 18px;
}
nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: var(--cf-radius-sm);
  color: var(--cf-text-2);
  text-decoration: none;
  font-size: 14px;
  transition: all 0.15s;
}
.nav-item:hover {
  background: var(--cf-hover);
  color: var(--cf-text-1);
}
.nav-item.active {
  background: var(--cf-active);
  color: var(--cf-bili-blue);
  font-weight: 600;
}
.nav-icon {
  font-size: 16px;
}
.main {
  flex: 1;
  min-width: 0;
  height: 100vh;
  overflow: hidden;
}
</style>
