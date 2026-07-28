import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The console is served same-origin by FastAPI in production, so the session
// cookie (HttpOnly, SameSite=Lax) just works and there is no CORS at all. The
// dev proxy below preserves that: `npm run dev` still talks to the same origin
// from the browser's point of view.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
