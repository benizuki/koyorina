<script setup lang="ts">
import { computed } from 'vue'
import { PALETTES, useThemeMode, type ThemeMode } from '@/composables/useThemeMode'

const { mode, setMode, palette, setPalette } = useThemeMode()

// 「システムに合わせる」を残す。既定がこれなので、外すと OS をダークにしている人に
// 毎回ダークを選ばせることになる。3つ目だが、押すのは一度きり。
const options: { value: ThemeMode; label: string; icon: string }[] = [
  { value: 'system', label: '自動', icon: 'mdi-theme-light-dark' },
  { value: 'light', label: 'ライト', icon: 'mdi-weather-sunny' },
  { value: 'dark', label: 'ダーク', icon: 'mdi-weather-night' },
]
const current = computed(() => options.find(item => item.value === mode.value) ?? options[0])
// 配色は明暗とは別の軸。同じ盤に並べるが、段を分けて混ぜない。
const currentPalette = computed(() =>
  PALETTES.find(item => item.id === palette.value) ?? PALETTES[0])
</script>

<template>
  <v-menu :close-on-content-click="false">
    <template #activator="{ props: activator }">
      <v-btn v-bind="activator" variant="text" size="small" class="theme-button"
        :prepend-icon="current.icon"
        :aria-label="`表示: ${current.label}・配色: ${currentPalette.japanese}。押すと選べます`">
        <span class="theme-label">{{ current.label }}</span>
      </v-btn>
    </template>
    <!-- 一覧ではなく盤にする。2つの軸を上下の段で見せたほうが、
         いまどの組み合わせなのかが一目で分かる。 -->
    <div class="board">
      <div class="row">
        <button v-for="item in options" :key="item.value" type="button" class="cell"
          :class="{ 'cell--on': item.value === mode }" @click="setMode(item.value)">
          <v-icon :icon="item.icon" size="16" />
          <span>{{ item.label }}</span>
        </button>
      </div>
      <!-- 配色は2行2列。横一列に伸ばすと、増えるたびに盤が横へ広がって
           アプリバーからはみ出す。段を増やすほうが幅が変わらない。 -->
      <div class="grid">
        <button v-for="item in PALETTES" :key="item.id" type="button" class="cell"
          :class="[`cell--${item.id}`, { 'cell--on': item.id === palette }]"
          :title="item.japanese" @click="setPalette(item.id)">
          <span class="swatch" />
          <span>{{ item.label }}</span>
        </button>
      </div>
    </div>
  </v-menu>
</template>

<style scoped>
/* 狭い画面では文字を畳んでアイコンだけにする。 */
@media (max-width: 900px) { .theme-label { display: none; } }

.board { background: var(--surface-card); border: 1px solid var(--border-default);
  border-radius: var(--radius-md); overflow: hidden; }
.row { display: flex; }
.row + .row, .row + .grid { border-top: 1px solid var(--border-default); }
/* 2列。行が増えても幅は変わらない。 */
.grid { display: grid; grid-template-columns: 1fr 1fr; }
.grid .cell:nth-child(n + 3) { border-top: 1px solid var(--border-default); }
.grid .cell:nth-child(even) { border-left: 1px solid var(--border-default); }
.grid .cell { justify-content: flex-start; }
.cell { flex: 1; display: flex; align-items: center; justify-content: center; gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-4); min-height: var(--control-md); white-space: nowrap;
  font-size: var(--fs-sm); color: var(--ink-2); }
.row .cell + .cell { border-left: 1px solid var(--border-default); }
.cell:hover { background: var(--surface-muted); }
.cell--on { background: var(--surface-accent); color: var(--text-brand);
  font-weight: var(--fw-medium); }

/* 配色は名前だけだと何色か分からない。実際の色を小さく添える。
   ここは配色そのものを選ぶ場所なので、いまのテーマの色では示せない。 */
.swatch { width: 10px; height: 10px; border-radius: 50%; flex: none;
  border: 1px solid var(--border-mid); }
.cell--green .swatch { background: var(--swatch-green); }  /* 常磐色 */
.cell--blue .swatch { background: var(--swatch-blue); }    /* 藍色 */
.cell--red .swatch { background: var(--swatch-red); }      /* 朱色 */
.cell--yellow .swatch { background: var(--swatch-yellow); }/* 黄朽葉色 */
</style>
