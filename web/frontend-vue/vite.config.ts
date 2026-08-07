import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// EP 按需自动导入照搬自 ChatFlow vite.config.ts:8-17；端口/代理沿用 DevPilot 5175→8000。
export default defineConfig({
  plugins: [
    vue(),
    AutoImport({ resolvers: [ElementPlusResolver()], dts: false }),
    Components({ resolvers: [ElementPlusResolver()], dts: false }),
  ],
  server: {
    host: '127.0.0.1',
    port: 5174,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true, changeOrigin: true },
    },
    watch: {
      ignored: ['**/venv/**', '**/*.py', '**/*.log', '**/src/devpilot/**'],
    },
  },
})
