import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vuetify from 'vite-plugin-vuetify'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [
    vue(),
    // 使ったコンポーネントだけを自動で取り込む。手書きの import が不要になる。
    vuetify({ autoImport: true }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    // 開発時は Vite(5173) と backend(8080) が別 Origin になる。
    // ここでプロキシしておくと、セッション Cookie がそのまま効く。
    // backend にパスを足したら、ここにも足すこと。
    proxy: {
      '/api':    'http://localhost:8080',
      '/auth':   'http://localhost:8080',
      '/login':  'http://localhost:8080',
      '/logout': 'http://localhost:8080',
    },
  },
  build: {
    // backend が ../frontend/dist を配信する前提。
    outDir: './dist',
    emptyOutDir: true,
  },
})
