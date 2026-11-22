import { defineConfig } from "vite";

// A sandboxed Electron preload cannot require the app’s local modules.
export default defineConfig({
  cacheDir: ".vite",
  build: {
    outDir: "dist-electron",
    emptyOutDir: false,
    lib: {
      entry: "electron/preload.ts",
      formats: ["cjs"],
      fileName: () => "preload.js",
    },
    rollupOptions: { external: ["electron"] },
  },
});
