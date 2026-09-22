import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 2199,
    proxy: {
      '/api': 'http://127.0.0.1:3456',
    },
  },
})
