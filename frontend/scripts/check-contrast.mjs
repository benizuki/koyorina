/**
 * tokens.css の色が WCAG AA（4.5:1）を満たしているかを確認する。
 *
 *   npm run check:contrast
 *
 * 配色を変えたら必ず走らせること。「--ink-1〜4 はどの surface の上でも読める」
 * という前提が崩れると、画面ごとに濃さを確かめる作業が復活する。
 * 依存は無い。Node だけで動く。
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const css = readFileSync(resolve(here, '../src/styles/tokens.css'), 'utf8')
const mainTs = readFileSync(resolve(here, '../src/main.ts'), 'utf8')

/**
 * トークンを light / dark の 2 面に展開する。
 *   --x: light-dark(#aaa, #bbb);  → light=#aaa, dark=#bbb
 *   --x: #ccc;                    → 両方 #ccc（テーマで変えない色）
 *
 * 配色（palette）ごとに別々へ集める。1つに混ぜると、あとから書いたほうだけが
 * 検査され、もう片方の保証が黙って消える。
 */
function parseBlock(text) {
  const light = {}
  const dark = {}
  for (const [, name, a, b] of text.matchAll(
    /(--[\w-]+):\s*light-dark\(\s*(#[0-9a-fA-F]{6})\s*,\s*(#[0-9a-fA-F]{6})\s*\)\s*;/g,
  )) {
    light[name] = a
    dark[name] = b
  }
  for (const [, name, hex] of text.matchAll(/(--[\w-]+):\s*(#[0-9a-fA-F]{6})\s*;/g)) {
    if (!(name in light)) {
      light[name] = hex
      dark[name] = hex
    }
  }
  return { light, dark }
}

/** 既定（緑系）と、その上書き（青系など）。上書きは既定に重ねて使う。 */
function readPalettes() {
  const blocks = [...css.matchAll(/:root(\[data-palette="([\w-]+)"\])?\s*\{([^}]*)\}/g)]
  const base = blocks.find((block) => !block[2])
  if (!base) throw new Error(':root ブロックが見つかりません')
  const green = parseBlock(base[3])
  const palettes = { green }
  for (const block of blocks) {
    if (!block[2]) continue
    const override = parseBlock(block[3])
    palettes[block[2]] = {
      light: { ...green.light, ...override.light },
      dark: { ...green.dark, ...override.dark },
    }
  }
  return palettes
}

const channel = (v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)

/** 色相（度）。状態どうしが見分けられるかの判定に使う。 */
function hue(hex) {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
  const max = Math.max(r, g, b)
  const span = max - Math.min(r, g, b)
  if (span === 0) return 0
  const raw = max === r ? (g - b) / span : max === g ? 2 + (b - r) / span : 4 + (r - g) / span
  return ((raw * 60) % 360 + 360) % 360
}

const apart = (a, b) => {
  const d = Math.abs(hue(a) - hue(b)) % 360
  return Math.min(d, 360 - d)
}

function luminance(hex) {
  const [r, g, b] = [1, 3, 5].map((i) => channel(parseInt(hex.slice(i, i + 2), 16) / 255))
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

const AA = 4.5
// 状態の色どうしの最小色相差。これを割ると「完了」と「エラー」が見分けられない。
const HUE_FLOOR = 20
// 状態テキストは「自分の薄い面」と「カード面」の両方で読める必要がある
const STATE_SURFACE = {
  brand: '--surface-accent',
  warn: '--surface-warn',
  danger: '--surface-danger',
  info: '--surface-info',
}

const palettes = readPalettes()
const failures = []
let current = ''

function check(label, value, floor = AA) {
  const ok = value >= floor
  if (!ok) failures.push(`${current} ${label} ${value.toFixed(2)}:1`)
  console.log(`${ok ? 'OK  ' : 'FAIL'} ${label.padEnd(28)} ${value.toFixed(2)}:1`)
}

// 配色 × ライト/ダーク の全組み合わせ。どれか1つでも割れば落とす。
for (const [palette, themes] of Object.entries(palettes)) {
for (const name of ['light', 'dark']) {
  const t = themes[name]
  if (Object.keys(t).length === 0) {
    console.error(`${palette}/${name} のトークンを 1 つも読めませんでした。tokens.css の書式を確認してください。`)
    process.exit(1)
  }
  current = `${palette}/${name}`
  console.log(`\n--- ${palette} / ${name} ---`)

  // ink-1〜4 は「読ませる面」すべての上で AA。solid と inverse は白文字用なので除く
  const surfaces = Object.entries(t).filter(
    ([k]) => k.startsWith('--surface-') && !k.includes('solid') && !k.includes('inverse'),
  )
  for (const ink of ['--ink-1', '--ink-2', '--ink-3', '--ink-4']) {
    const worst = surfaces.reduce(
      (acc, [k, v]) => (contrast(t[ink], v) < acc.ratio ? { ratio: contrast(t[ink], v), on: k } : acc),
      { ratio: Infinity, on: '' },
    )
    check(`${ink} on ${worst.on.replace('--surface-', '')}`, worst.ratio)
  }

  for (const [state, surfaceToken] of Object.entries(STATE_SURFACE)) {
    const text = t[`--text-${state}`]
    check(
      `text-${state}`,
      Math.min(contrast(text, t[surfaceToken]), contrast(text, t['--surface-card'])),
    )
  }

  // ログ・差分の面。ここには ink だけでなく状態の色も文字として載る。
  // surface-* を ink でしか見ていなかったため、ライトで色付きの行が
  // 4.0:1 まで落ちているのに気づけなかった。文字として載る色は全部見る。
  for (const token of ['--ink-2', '--ink-4', '--text-brand', '--text-warn', '--text-danger']) {
    check(`${token.replace('--', '')} on sunken`, contrast(t[token], t['--surface-sunken']))
  }

  for (const state of ['accent', 'warn', 'danger', 'info', 'notice']) {
    check(`on-solid / ${state}`, contrast(t['--text-on-solid'], t[`--surface-${state}-solid`]))
  }

  check('on-inverse', contrast(t['--text-on-inverse'], t['--surface-inverse']))

  // 状態どうしが見分けられること。赤系はブランドが赤なので、危険色と衝突しうる。
  // 既定（緑系）の最小間隔は warn と danger の 21°。それを下回らせない。
  for (const [a, b] of [['brand', 'danger'], ['brand', 'warn'], ['warn', 'danger']]) {
    const degrees = apart(t[`--text-${a}`], t[`--text-${b}`])
    const ok = degrees >= HUE_FLOOR
    if (!ok) failures.push(`${current} ${a}/${b} の色相差 ${degrees.toFixed(0)}°`)
    console.log(`${ok ? 'OK  ' : 'FAIL'} ${`${a} vs ${b}`.padEnd(28)} ${degrees.toFixed(0)}°`)
  }

  // ink-muted は AA を満たさない前提の装飾用トークン。
  // 満たしてしまった場合も知らせる（文字に使ってよいと誤解されるため）
  const mutedWorst = surfaces.reduce((min, [, v]) => Math.min(min, contrast(t['--ink-muted'], v)), Infinity)
  console.log(`INFO --ink-muted は ${mutedWorst.toFixed(2)}:1（装飾専用。文字に使わない）`)
}
}

/**
 * Vuetify のテーマ色（main.ts）が tokens.css と一致しているか。
 *
 * Vuetify は on-primary などを自前で計算するため hex を直接渡す必要があり、
 * 同じ値が 2 か所に出る。片方だけ直すと、v-btn の色と手書き CSS の色が
 * 少しずつ違う画面になる。人が気をつけるのではなく、ここで確かめる。
 */
const VUETIFY_MAP = {
  primary: '--surface-accent-solid',
  secondary: '--surface-inverse',
  surface: '--surface-card',
  background: '--surface-page',
  // 状態色は塗りつぶしではなく文字の色を渡す。v-alert の既定が tonal で、
  // 渡した色がそのまま文字色になるため。塗りつぶし用の濃い色を渡すと、
  // ダークで「エラーです」の赤が地に沈んで読めない（実際にそうなっていた）。
  error: '--text-danger',
  warning: '--text-warn',
  info: '--text-info',
  success: '--text-brand',
}
const MAPS = { green: 'themeColors', blue: 'bluePalette', red: 'redPalette',
               yellow: 'yellowPalette' }

console.log('\n--- main.ts と tokens.css の一致 ---')
for (const [palette, symbol] of Object.entries(MAPS)) {
  const body = mainTs.slice(mainTs.indexOf(`const ${symbol} = {`))
  for (const [index, mode] of ['light', 'dark'].entries()) {
    const section = body.slice(body.indexOf(`${mode}: {`), body.indexOf('}', body.indexOf(`${mode}: {`)))
    for (const [, key, hex] of section.matchAll(/(\w+):\s*'(#[0-9a-fA-F]{6})'/g)) {
      const token = VUETIFY_MAP[key]
      const want = palettes[palette][mode][token]
      if (hex.toLowerCase() !== want.toLowerCase()) {
        failures.push(`${palette}/${mode} main.ts の ${key} が ${hex}、tokens.css の ${token} は ${want}`)
        console.log(`FAIL ${palette}/${mode} ${key} ${hex} != ${want}`)
      }
    }
    void index
  }
}
if (!failures.some((f) => f.includes('main.ts'))) console.log('OK   すべて一致しています')

// 配色を選ぶボタンの見本も、その配色の実物と一致していること。
// 見本だけ古いと、押すまで何色になるか分からない選択肢になる。
console.log('\n--- 配色の見本 ---')
for (const palette of Object.keys(palettes)) {
  for (const mode of ['light', 'dark']) {
    const shown = palettes.green[mode][`--swatch-${palette}`]
    const real = palettes[palette][mode]['--surface-accent-solid']
    if (shown !== real) {
      failures.push(`${palette}/${mode} 見本 ${shown} が実物 ${real} と違います`)
      console.log(`FAIL ${palette}/${mode} ${shown} != ${real}`)
    }
  }
}
if (!failures.some((f) => f.includes('見本'))) console.log('OK   すべて実物と一致しています')

if (failures.length > 0) {
  console.error(`\n${failures.length} 件が基準（AA 4.5:1 / 色相差 ${HUE_FLOOR}°）を満たしていません:`)
  for (const f of failures) console.error(`  - ${f}`)
  process.exit(1)
}
console.log('\nすべて WCAG AA を満たしています。')
