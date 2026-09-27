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
import { APP_NAME } from './branding'

document.title = `${APP_NAME} — アプリ作成`

// Vuetify のテーマ色は styles/tokens.css の写し。
// Vuetify は on-primary などを自前で計算するため、hex を直接渡す必要がある。
// ここと tokens.css は必ず同じ値に保つこと（片方だけ変えると、
// v-btn の色と手書き CSS の色が少しずつ違う画面ができる）。
const themeColors = {
  light: {
    primary:    '#238056', // = --surface-accent-solid
    secondary:  '#0f514b', // = --surface-inverse
    surface:    '#ffffff', // = --surface-card
    background: '#e3e9e1', // = --surface-page
    error:      '#b93a2b', // = --text-danger
    warning:    '#9b5b25', // = --text-warn
    info:       '#416996', // = --text-info
    success:    '#217751', // = --text-brand
  },
  dark: {
    primary:    '#1a7c49', // = --surface-accent-solid
    secondary:  '#082220', // = --surface-inverse
    surface:    '#142f37', // = --surface-card
    background: '#0a201c', // = --surface-page
    error:      '#fb8b7f', // = --text-danger
    warning:    '#fbc97e', // = --text-warn
    info:       '#8bbff7', // = --text-info
    success:    '#54d297', // = --text-brand
  },
} as const

// 青系・赤系・黄系で差し替える分だけ。tokens.css の :root[data-palette=...] と同じ値にすること。
// ずれていないかは npm run check:contrast が確かめる。
const bluePalette = {
  light: {
    primary:    '#286a8a', // = --surface-accent-solid
    secondary:  '#0f3551', // = --surface-inverse
    background: '#e1e8e9', // = --surface-page
    success:    '#276483', // = --text-brand
  },
  dark: {
    primary:    '#1a5d7c', // = --surface-accent-solid
    secondary:  '#081722', // = --surface-inverse
    surface:    '#142137', // = --surface-card
    background: '#050e12', // = --surface-page
    success:    '#5aaed5', // = --text-brand
  },
} as const

const redPalette = {
  light: {
    primary:    '#ba411c', // = --surface-accent-solid
    secondary:  '#5c2d11', // = --surface-inverse
    background: '#e8e2e2', // = --surface-page
    error:      '#bd2d63', // = --text-danger
    warning:    '#8b641e', // = --text-warn
    success:    '#b73e1f', // = --text-brand
  },
  dark: {
    primary:    '#b45129', // = --surface-accent-solid
    secondary:  '#291913', // = --surface-inverse
    surface:    '#38261f', // = --surface-card
    background: '#261812', // = --surface-page
    error:      '#f08ea6', // = --text-danger
    warning:    '#f5cb89', // = --text-warn
    success:    '#fa9b77', // = --text-brand
  },
} as const

const yellowPalette = {
  light: {
    primary:    '#896721', // = --surface-accent-solid
    secondary:  '#554321', // = --surface-inverse
    background: '#ebe6df', // = --surface-page
    error:      '#ae4062', // = --text-danger
    warning:    '#9d564d', // = --text-warn
    success:    '#806020', // = --text-brand
  },
  dark: {
    primary:    '#876109', // = --surface-accent-solid
    secondary:  '#241c0d', // = --surface-inverse
    surface:    '#332918', // = --surface-card
    background: '#221a0d', // = --surface-page
    error:      '#f08ea6', // = --text-danger
    warning:    '#ffbbae', // = --text-warn
    success:    '#dfae50', // = --text-brand
  },
} as const

// 操作への反応の濃さ。Vuetifyの既定は hover 0.04 で、色が変わったことに気づけない。
// 押せるものだと分かることは好みの問題ではないので、既定の3倍強まで上げる。
// 重なりの色はボタン自身の文字色なので、削除ボタンなら赤、通常なら文字色で濃くなる。
// 値は Vuetify のテーマ変数（--v-hover-opacity など）へ深い結合で混ざる。
const interaction = {
  'hover-opacity': 0.14,
  'focus-opacity': 0.16,
  'activated-opacity': 0.16,
}

const vuetify = createVuetify({
  components,
  directives,
  theme: {
    // 起動直後の値。App.vue が useThemeMode の結果で即座に上書きするので、
    // ここを dark にしても挙動は変わらない。
    // 実際の既定は「システムに従う」（composables/useThemeMode.ts）。
    defaultTheme: 'light',
    themes: {
      light: { dark: false, colors: { ...themeColors.light }, variables: { ...interaction } },
      dark:  { dark: true,  colors: { ...themeColors.dark },  variables: { ...interaction } },
      // 配色は明暗とは別の軸。Vuetify はテーマ名1つで選ぶので、掛け合わせを名前にする。
      'blue-light': { dark: false, colors: { ...themeColors.light, ...bluePalette.light },
                      variables: { ...interaction } },
      'blue-dark':  { dark: true,  colors: { ...themeColors.dark, ...bluePalette.dark },
                      variables: { ...interaction } },
      'red-light':  { dark: false, colors: { ...themeColors.light, ...redPalette.light },
                      variables: { ...interaction } },
      'red-dark':   { dark: true,  colors: { ...themeColors.dark, ...redPalette.dark },
                      variables: { ...interaction } },
      'yellow-light': { dark: false, colors: { ...themeColors.light, ...yellowPalette.light },
                        variables: { ...interaction } },
      'yellow-dark':  { dark: true,  colors: { ...themeColors.dark, ...yellowPalette.dark },
                        variables: { ...interaction } },
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
    VCombobox:  { variant: 'outlined', density: 'comfortable', hideDetails: 'auto' },
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
