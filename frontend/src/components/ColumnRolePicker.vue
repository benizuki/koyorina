<script setup lang="ts">
import type { ColumnRole } from '@/types'
import { roleGroups as groups } from '@/columnRoles'

// 項目が多いので、プルダウンではなくマス目で並べる。IT に慣れていない人でも
// 選択肢の全体が一度に見え、押す場所が大きい。
const role = defineModel<ColumnRole>({ required: true })

</script>

<template>
  <div class="role-picker">
    <section v-for="group in groups" :key="group.title" class="role-group">
      <h4>{{ group.title }}</h4>
      <div class="role-grid" role="radiogroup" :aria-label="group.title">
        <button v-for="option in group.options" :key="option.value" type="button" class="role-tile"
          role="radio" :aria-checked="role === option.value" :class="{ 'is-selected': role === option.value }"
          @click="role = option.value">
          <v-icon :icon="role === option.value ? 'mdi-check-circle' : option.icon" size="small" />
          <span>{{ option.title }}</span>
        </button>
      </div>
    </section>
  </div>
</template>

<style scoped>
/* 選択肢の範囲が一目で分かるよう、面の色を変える。マスはカードの色で浮かせる。 */
.role-picker { display: flex; flex-direction: column; gap: var(--sp-4); padding: var(--sp-4);
  background: var(--surface-muted); border: 1px solid var(--border-default); border-radius: var(--radius-md); }
.role-group h4 { font-size: var(--fs-sm); font-weight: var(--fw-bold); color: var(--ink-2); margin-bottom: var(--sp-2); }
.role-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(8rem, 1fr)); gap: var(--sp-2); }
.role-tile { display: flex; align-items: center; gap: var(--sp-2); min-height: var(--control-lg);
  padding: var(--sp-2) var(--sp-3); text-align: left; cursor: pointer;
  color: var(--ink-2); background: var(--surface-card);
  border: 1px solid var(--border-default); border-radius: var(--radius-md); font-size: var(--fs-sm); }
.role-tile:hover { background: var(--surface-accent); border-color: var(--border-brand); color: var(--ink-1); }
.role-tile:focus-visible { outline: 2px solid var(--border-brand); outline-offset: 2px; }
/* 選択中は塗りつぶし。文字は --text-on-solid だけを使う（同系色の文字は背景に溶ける）。 */
.role-tile.is-selected { color: var(--text-on-solid); background: var(--surface-accent-solid);
  border-color: var(--surface-accent-solid); font-weight: var(--fw-bold); }
</style>
