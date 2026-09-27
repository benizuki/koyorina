<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { PreviewStatus } from '@/types'
const props = defineProps<{ status?: PreviewStatus }>()
const reloads = ref(0)
// 再起動の前後で同じ画面を映し続けると、止まっていた側が白いまま残る。
// 実行中に変わった時点で読み直す。
watch(() => props.status?.state, (now, before) => {
  if (now === 'running' && before && before !== 'running') reloads.value += 1
})
const frameKey = computed(() => `${props.status?.updated_at ?? ''}-${reloads.value}`)
</script>

<template>
  <div class="frame">
    <template v-if="status?.state === 'running' && status.url">
      <div class="bar">
        <span class="chip chip--pill chip--brand">実行中</span>
        <v-btn variant="text" size="small" prepend-icon="mdi-refresh"
          @click="reloads += 1">画面を更新</v-btn>
        <a :href="status.url" target="_blank" rel="noopener">別のタブで開く</a>
      </div>
      <iframe :key="frameKey" :src="status.url" title="開発中のアプリ" />
    </template>
    <div v-else-if="status?.state === 'failed'" class="failure">
      <p class="tip tip--danger">
        <v-icon icon="mdi-alert-circle-outline" />
        <span>{{ status.message || 'プレビューが起動できませんでした。' }}</span>
      </p>
      <p v-if="status.hint" class="my-3">{{ status.hint }}</p>
      <pre v-if="status.evidence" class="evidence">{{ status.evidence }}</pre>
      <p class="tip tip--compact">実行管理タブから、もう一度起動したりログを見たりできます。</p>
    </div>
    <p v-else-if="status?.state === 'starting'" class="tip tip--warn">
      起動中です。実行管理タブで進み具合を確認できます。起動できたらこの画面に表示します。
    </p>
    <p v-else class="tip">
      まだ動いていません。実行管理タブの「プレビューを開始」を押すと、ここで動かせます。
    </p>
  </div>
</template>

<style scoped>
.frame { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.bar { display: flex; gap: var(--sp-3); align-items: center; margin-bottom: var(--sp-3); }
.failure { overflow: auto; }
/* ログの1行はそのまま出す。折り返さないと原因の末尾が読めない。 */
.evidence { background: var(--surface-sunken); border: 1px solid var(--border-subtle);
  color: var(--ink-2); padding: var(--sp-3);
  border-radius: var(--radius-md); font-size: var(--fs-xs); white-space: pre-wrap; word-break: break-all; }
iframe { flex: 1; width: 100%; min-height: 0; border: 0; border-radius: var(--radius-md); background: #fff; }
</style>
