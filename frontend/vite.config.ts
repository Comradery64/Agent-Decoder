import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: path.resolve(__dirname, "../src/agent_decoder/static"),
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // vite 8 / rolldown requires manualChunks to be a function — the
        // object form is no longer accepted. This functional form also
        // works under the older rollup-based vite 5/6/7.
        manualChunks(id: string) {
          if (id.includes("node_modules/echarts/")) return "echarts";
          if (id.includes("node_modules/react/") || id.includes("node_modules/react-dom/")) return "react";
          return undefined;
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:4242",
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
  },
});
