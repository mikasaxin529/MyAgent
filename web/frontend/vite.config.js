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
        },
    },
});
