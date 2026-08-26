import {defineConfig} from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: '/',
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8501',
      '/sse': {target: 'http://127.0.0.1:8501', ws: false},
    },
  },
  build: {
    outDir: 'dist',
    rollupOptions: {
      output: {
        manualChunks: {
          monaco: ['monaco-editor', '@monaco-editor/react'],
          react: ['react', 'react-dom', 'react-router-dom'],
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['src/test-setup.ts'],
    fileParallelism: false,
    exclude: ['node_modules/**', 'dist/**', 'tests/visual/**', 'tests/e2e/**'],
  },
})
