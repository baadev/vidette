import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8642",
      "/healthz": "http://localhost:8642",
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
