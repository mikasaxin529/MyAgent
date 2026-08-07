import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import 'element-plus/dist/index.css'
import 'highlight.js/styles/github-dark.css'
import './style.css'
import App from './App.vue'
import { router } from './router'

const app = createApp(App)
// 注册所有 Element Plus 图标为全局组件（照搬 ChatFlow main.ts:12-14）
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}
app.use(ElementPlus)
app.use(router)
app.mount('#app')
