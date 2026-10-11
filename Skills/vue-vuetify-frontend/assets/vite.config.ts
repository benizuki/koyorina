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
  // Vuetify 4 は重なり順（z-index）を1つの共有状態で数える。開発時に事前バンドルと
  // 生の ESM が混ざると共有状態が2つになり、ダイアログ内のメニューが背面に隠れる。
  // 重なる部品をまとめて事前バンドルさせる（Vuetify の移行ガイドのとおり）。
  optimizeDeps: {
    include: [
      'vuetify/components/VOverlay',
      'vuetify/components/VDialog',
      'vuetify/components/VMenu',
      'vuetify/components/VSelect',
      'vuetify/components/VTooltip',
    ],
  },
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
