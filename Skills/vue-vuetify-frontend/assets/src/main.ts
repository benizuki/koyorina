import { createApp } from 'vue'
import { createVuetify } from 'vuetify'
import { createRouter, createWebHistory } from 'vue-router'
import * as components from 'vuetify/components'
import * as directives from 'vuetify/directives'
import 'vuetify/styles'
import '@mdi/font/css/materialdesignicons.css'

// トークンを先に読む。base.css がトークンを参照するので順番を入れ替えない。
import './styles/tokens.css'
import './styles/base.css'

import App from './App.vue'
import HomeView from './views/HomeView.vue'

// Vuetify のテーマ色は styles/tokens.css の写し。
// Vuetify は on-primary などを自前で計算するため、hex を直接渡す必要がある。
// ここと tokens.css は必ず同じ値に保つこと（片方だけ変えると、
// v-btn の色と手書き CSS の色が少しずつ違う画面ができる）。
const themeColors = {
  light: {
    primary:    '#347e72', // = --surface-accent-solid
    secondary:  '#173f49', // = --surface-inverse
    surface:    '#ffffff', // = --surface-card
    background: '#f4f6f2', // = --surface-page
    error:      '#c25247', // = --surface-danger-solid
    warning:    '#b06122', // = --surface-warn-solid
    info:       '#4e72ca', // = --surface-info-solid
    success:    '#347e72', // = --surface-accent-solid
  },
  dark: {
    primary:    '#63c3b1',
    secondary:  '#0b1b1f',
    surface:    '#16292e',
    background: '#0f1f23',
    error:      '#f0897d',
    warning:    '#aa652f',
    info:       '#5674bb',
    success:    '#63c3b1',
  },
} as const

const vuetify = createVuetify({
  components,
  directives,
  theme: {
    // 起動直後の値。App.vue が useThemeMode の結果で即座に上書きするので、
    // ここを dark にしても挙動は変わらない。
    // 実際の既定は「システムに従う」（composables/useThemeMode.ts）。
    defaultTheme: 'light',
    themes: {
      light: { dark: false, colors: { ...themeColors.light } },
      dark:  { dark: true,  colors: { ...themeColors.dark } },
    },
  },

  // 部品の既定値をここで決めるのが、見た目を揃えるいちばん確実な方法。
  // 画面ごとに variant や density を書かせると、必ずばらける。
  // 個別に上書きしたくなったら、まず「本当にその画面だけ違ってよいか」を疑う。
  defaults: {
    VBtn:       { rounded: 'lg', elevation: 0, variant: 'flat' },
    VCard:      { rounded: 'lg', elevation: 0, border: true },
    VDialog:    { maxWidth: 720 },
    // hideDetails: 'auto' を付けないと、エラーが無いときも
    // 説明文の高さが確保され、フォームの行間が画面ごとにずれる。
    VTextField: { variant: 'outlined', density: 'comfortable', hideDetails: 'auto' },
    VTextarea:  { variant: 'outlined', density: 'comfortable', hideDetails: 'auto' },
    VSelect:    { variant: 'outlined', density: 'comfortable', hideDetails: 'auto' },
    VAutocomplete: { variant: 'outlined', density: 'comfortable', hideDetails: 'auto' },
    VCheckbox:  { density: 'comfortable', hideDetails: 'auto' },
    VDataTable: { density: 'comfortable', itemsPerPage: 20 },
    VChip:      { size: 'small', variant: 'tonal' },
    VAlert:     { density: 'compact', variant: 'tonal' },
    VTooltip:   { location: 'top' },
  },
})

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/',                component: HomeView },
    // 未知のパスはトップに戻す。backend 側の SPA フォールバックと役割が揃う。
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

createApp(App).use(vuetify).use(router).mount('#app')
