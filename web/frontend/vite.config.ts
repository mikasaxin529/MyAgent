import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// DevPilot frontend dev server.
// /api and /ws are proxied to the FastAPI backend on port 8000.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://localhost:8000",
        ws: true,
        changeOrigin: true,
      },
      // 交付物文件（/files/<agent>/<session>/<name>）也走后端，否则 dev 下下载 404
      "/files": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
});
