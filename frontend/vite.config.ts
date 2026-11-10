import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev the UI runs on :5173 and proxies API calls to FastAPI on :8000.
// In production FastAPI serves the built files itself (same origin, no CORS).
const api = "http://127.0.0.1:8000";
export default defineConfig({
  plugins: [react()],
  // The API already owns /assets (signed source images), so bundles go in /static.
  build: { assetsDir: "static" },
  server: {
    proxy: {
      "/auth": api,
      "/users": api,
      "/documents": api,
      "/chat": api,
      "/assets": api,
      "/health": api,
    },
  },
});
