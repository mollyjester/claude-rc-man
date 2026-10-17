import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// During development the Vite server forwards /api to a backend on port 8000, the Terminal
// tab's WebSocket included.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": { target: "http://127.0.0.1:8000", ws: true } } },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
