import { createRouter, createWebHistory } from 'vue-router'
import Chat from './pages/Chat.vue'
import Run from './pages/Run.vue'
import Eval from './pages/Eval.vue'
import Skills from './pages/Skills.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: Chat },
    { path: '/run', component: Run },
    { path: '/eval', component: Eval },
    { path: '/skills', component: Skills },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})
