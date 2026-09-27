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

/**
 * トークンを light / dark の 2 面に展開する。
 *   --x: light-dark(#aaa, #bbb);  → light=#aaa, dark=#bbb
 *   --x: #ccc;                    → 両方 #ccc（テーマで変えない色）
 */
function readTokens() {
  const light = {}
  const dark = {}
  for (const [, name, a, b] of css.matchAll(
    /(--[\w-]+):\s*light-dark\(\s*(#[0-9a-fA-F]{6})\s*,\s*(#[0-9a-fA-F]{6})\s*\)\s*;/g,
  )) {
    light[name] = a
    dark[name] = b
  }
  for (const [, name, hex] of css.matchAll(/(--[\w-]+):\s*(#[0-9a-fA-F]{6})\s*;/g)) {
    if (!(name in light)) {
      light[name] = hex
      dark[name] = hex
    }
  }
  return { light, dark }
}

const channel = (v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)

function luminance(hex) {
  const [r, g, b] = [1, 3, 5].map((i) => channel(parseInt(hex.slice(i, i + 2), 16) / 255))
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

const AA = 4.5
// 状態テキストは「自分の薄い面」と「カード面」の両方で読める必要がある
const STATE_SURFACE = {
  brand: '--surface-accent',
  warn: '--surface-warn',
  danger: '--surface-danger',
  info: '--surface-info',
}

const themes = readTokens()
const failures = []

function check(label, value, floor = AA) {
  const ok = value >= floor
  if (!ok) failures.push(`${label} ${value.toFixed(2)}:1`)
  console.log(`${ok ? 'OK  ' : 'FAIL'} ${label.padEnd(28)} ${value.toFixed(2)}:1`)
}

for (const name of ['light', 'dark']) {
  const t = themes[name]
  if (Object.keys(t).length === 0) {
    console.error(`${name} のトークンを 1 つも読めませんでした。tokens.css の書式を確認してください。`)
    process.exit(1)
  }
  console.log(`\n--- ${name} ---`)

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

  for (const state of ['accent', 'warn', 'danger', 'info']) {
    check(`on-solid / ${state}`, contrast(t['--text-on-solid'], t[`--surface-${state}-solid`]))
  }

  check('on-inverse', contrast(t['--text-on-inverse'], t['--surface-inverse']))

  // ink-muted は AA を満たさない前提の装飾用トークン。
  // 満たしてしまった場合も知らせる（文字に使ってよいと誤解されるため）
  const mutedWorst = surfaces.reduce((min, [, v]) => Math.min(min, contrast(t['--ink-muted'], v)), Infinity)
  console.log(`INFO --ink-muted は ${mutedWorst.toFixed(2)}:1（装飾専用。文字に使わない）`)
}

if (failures.length > 0) {
  console.error(`\n${failures.length} 件が AA(4.5:1) 未満です:`)
  for (const f of failures) console.error(`  - ${f}`)
  process.exit(1)
}
console.log('\nすべて WCAG AA を満たしています。')
