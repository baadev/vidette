import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // ws: true — the live event stream (/api/v1/ws) needs the upgrade proxied in dev;
      // in production the app is served by vidette itself, same origin, no proxy involved.
      "/api": { target: "http://localhost:8642", ws: true },
      "/healthz": "http://localhost:8642",
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
