import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Development: `npm run dev` serves the UI on http://localhost:5173 and forwards /api to the local backend
// (started with `python -m stockdata.web serve --dev`). Production: `npm run build` writes dist/, which the
// backend serves from a single URL.
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: { '/api': { target: 'http://127.0.0.1:8765', changeOrigin: false } },
  },
  build: { outDir: 'dist', sourcemap: false, chunkSizeWarningLimit: 900 },
})
