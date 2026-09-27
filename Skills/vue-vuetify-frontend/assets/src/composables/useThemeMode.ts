import { computed, ref, readonly } from 'vue'

/**
 * ライト / ダークの切り替え。
 *
 * 3 状態にしているのが肝。「システムに従う」を既定にしておくと、
 * 端末をダークにしている人は何も操作しなくても最初からダークで開ける。
 * light / dark の 2 状態だけだと、必ずどちらかを押させることになる。
 */
export type ThemeMode = 'system' | 'light' | 'dark'
/** 配色。明暗とは別の軸。組み合わせは 3 × 4 で、どれも tokens.css が保証する。
 *  基準にした和色の意味をそのまま名前にする。
 *  和名は読み上げと説明のために持つ。並べたときに幅が揃うのは英名のほう。
 *  並び順がそのまま選択盤の並び（2行2列）になる。 */
export type Palette = 'green' | 'blue' | 'red' | 'yellow'
export const PALETTES: { id: Palette; label: string; japanese: string }[] = [
  { id: 'green', label: 'EverGreen', japanese: '常磐色' },
  { id: 'blue', label: 'Indigo', japanese: '藍色' },
  { id: 'red', label: 'Vermilion', japanese: '朱色' },
  { id: 'yellow', label: 'Amber', japanese: '黄朽葉色' },
]

const STORAGE_KEY = 'theme-mode'
const PALETTE_KEY = 'theme-palette'

// モジュールスコープの ref。useThemeMode() をどこから何度呼んでも同じ状態を指す。
// テーマは画面ごとに別々だと困るため。
const mode = ref<ThemeMode>(readStoredMode())
const palette = ref<Palette>(readStoredPalette())

// OS 側の設定。ユーザーが OS のテーマを切り替えたら追随させる。
const query = typeof window !== 'undefined' && window.matchMedia
  ? window.matchMedia('(prefers-color-scheme: dark)')
  : null
const systemIsDark = ref(query?.matches ?? false)
query?.addEventListener('change', (e) => { systemIsDark.value = e.matches })

/** 実際に適用されるテーマ。Vuetify のテーマ名としてもそのまま使う。 */
const effectiveTheme = computed<'light' | 'dark'>(() =>
  mode.value === 'system' ? (systemIsDark.value ? 'dark' : 'light') : mode.value,
)

function readStoredPalette(): Palette {
  try {
    const stored = localStorage.getItem(PALETTE_KEY)
    if (PALETTES.some((item) => item.id === stored)) return stored as Palette
  } catch {
    // 読めなくても既定で表示できる。握りつぶしてよい。
  }
  return 'green'
}

function applyPalette(next: Palette): void {
  const root = document.documentElement
  // 既定は属性なし。増やしたときに「既定＝素の :root」の関係を保つため。
  if (next === 'green') root.removeAttribute('data-palette')
  else root.setAttribute('data-palette', next)
  try {
    localStorage.setItem(PALETTE_KEY, next)
  } catch {
    // 保存できなくても表示は切り替わる。
  }
}

function readStoredMode(): ThemeMode {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored === 'light' || stored === 'dark' || stored === 'system') return stored
  } catch {
    // プライベートモードなどで localStorage が読めないことがある。
    // 既定に倒すだけでよいので握りつぶす。
  }
  return 'system'
}

function applyMode(next: ThemeMode): void {
  const root = document.documentElement
  if (next === 'system') {
    // 属性を消すと tokens.css の color-scheme: light dark が効き、OS に従う。
    root.removeAttribute('data-theme')
  } else {
    root.setAttribute('data-theme', next)
  }
  try {
    localStorage.setItem(STORAGE_KEY, next)
  } catch {
    // 保存できなくても表示は切り替わる。次回開いたときに既定へ戻るだけ。
  }
}

export function useThemeMode() {
  function setMode(next: ThemeMode): void {
    mode.value = next
    applyMode(next)
  }

  /** system → light → dark → system の順に回す。ボタン 1 つで 3 状態を選べる。 */
  function cycleMode(): void {
    const order: ThemeMode[] = ['system', 'light', 'dark']
    setMode(order[(order.indexOf(mode.value) + 1) % order.length])
  }

  function setPalette(next: Palette): void {
    palette.value = next
    applyPalette(next)
  }

  return {
    mode: readonly(mode),
    palette: readonly(palette),
    effectiveTheme,
    setMode,
    cycleMode,
    setPalette,
  }
}
