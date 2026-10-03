import { defineConfig } from "vite";

export default defineConfig({
  server: { host: "127.0.0.1", port: 5186, proxy: { "/api": "http://127.0.0.1:8786" } },
  build: { outDir: "dist" },
});
