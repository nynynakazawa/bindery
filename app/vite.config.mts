import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "./",
  cacheDir: ".vite",
  server: {
    port: 5173,
    strictPort: true,
    // React Fast Refresh injects an inline script, which the app CSP blocks.
    hmr: false,
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
