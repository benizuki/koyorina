<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useNetworkAudit } from '@/composables/useNetworkAudit'

const minutes = ref(15)
const verdict = ref('all')
const { summary, loading, error, refresh } = useNetworkAudit()
const windows = [
  { title: '直近5分', value: 5 },
  { title: '直近15分', value: 15 },
  { title: '直近60分', value: 60 },
]
const verdicts = [
  { title: 'すべて', value: 'all' },
  { title: 'ブロック', value: 'blocked' },
  { title: '通信成立', value: 'allowed' },
  { title: 'リセット', value: 'reset' },
]
const resultView = {
  allowed: { label: '通信成立', color: 'success', icon: 'mdi-check-circle-outline' },
  blocked: { label: 'ブロック', color: 'error', icon: 'mdi-block-helper' },
  reset: { label: 'リセット', color: 'warning', icon: 'mdi-connection' },
  unknown: { label: '確認中', color: 'warning', icon: 'mdi-help-circle-outline' },
} as const
function shortTime(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('ja-JP', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).format(date)
}
function update() { return refresh(minutes.value, verdict.value) }
watch([minutes, verdict], update)
onMounted(update)
</script>

<template>
  <v-card class="pa-6 mb-5 audit-card">
    <div class="page-heading audit-heading">
      <div>
        <p class="eyebrow">NETWORK AUDIT</p>
        <h2>生成AIの通信状況</h2>
      </div>
      <div class="audit-actions">
        <v-select v-model="minutes" :items="windows" label="表示期間" density="compact"
          hide-details class="audit-select" />
        <v-select v-model="verdict" :items="verdicts" label="通信結果" density="compact"
          hide-details class="audit-select" />
        <v-btn variant="text" size="small" prepend-icon="mdi-refresh" :loading="loading"
          @click="update">更新</v-btn>
      </div>
    </div>
    <p class="mt-3 mb-4">接続開始と拒否を同じ経路ごとにまとめています。Pod名とFQDNを優先して表示します。</p>
    <v-alert v-if="error" type="warning" density="compact" class="mb-4">{{ error }}</v-alert>
    <p v-else-if="summary && !summary.available" class="tip tip--warn">
      通信監査readerがまだ配備されていません。監査Collectorを更新してください。
    </p>
    <template v-else>
      <div class="audit-counts mb-4">
        <span class="chip chip--brand"><v-icon size="x-small" icon="mdi-check-circle-outline" />
          通信成立 {{ summary?.totals.allowed ?? 0 }}</span>
        <span class="chip" :class="(summary?.totals.blocked ?? 0) ? 'chip--warn' : ''">
          <v-icon size="x-small" icon="mdi-block-helper" /> ブロック {{ summary?.totals.blocked ?? 0 }}</span>
        <span v-if="summary?.totals.reset" class="chip chip--warn">
          <v-icon size="x-small" icon="mdi-connection" /> リセット {{ summary.totals.reset }}</span>
        <span class="meta">集約前 {{ summary?.observed ?? 0 }}件</span>
        <span v-if="summary?.readers" class="meta">reader {{ summary.readers.available }}/{{ summary.readers.total }}</span>
      </div>
      <v-table fixed-header height="calc(100vh - 470px)" density="compact" class="audit-table">
        <thead><tr><th>最終確認</th><th>送信元</th><th>宛先</th><th>プロトコル</th>
          <th>通信結果</th><th class="numeric">観測数</th></tr></thead>
        <tbody>
          <tr v-for="row in summary?.rows ?? []"
            :key="`${row.source}-${row.destination}-${row.protocol}-${row.result}`"
            :class="row.result === 'blocked' ? 'blocked-row' : ''">
            <td class="nowrap">{{ shortTime(row.last_seen) }}</td>
            <td class="endpoint">{{ row.source }}</td>
            <td class="endpoint"><strong>{{ row.destination }}</strong>
              <span v-if="row.destination_ip" class="ip">{{ row.destination_ip }}</span></td>
            <td class="nowrap">{{ row.protocol }}<span v-if="row.port" class="port"> :{{ row.port }}</span></td>
            <td><v-chip size="x-small" variant="tonal" :color="resultView[row.result].color"
              :prepend-icon="resultView[row.result].icon" :title="row.reason">
              {{ resultView[row.result].label }}</v-chip></td>
            <td class="numeric">{{ row.count }}</td>
          </tr>
          <tr v-if="!loading && !(summary?.rows.length)"><td colspan="6" class="empty">
            この期間に該当する通信はありません。</td></tr>
        </tbody>
      </v-table>
      <p v-if="summary?.truncated" class="tip tip--warn mt-3">表示上限を超えています。期間または通信結果を絞ってください。</p>
      <p class="meta mt-3">この一覧は直近の状況確認用です。</p>
    </template>
  </v-card>
</template>

<style scoped>
.audit-heading { align-items: end; }
.eyebrow { color: var(--text-brand); font-size: var(--fs-2xs); font-weight: var(--fw-bold); letter-spacing: .12em; }
.audit-actions { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.audit-select { width: 150px; flex: 0 0 150px; }
.audit-counts { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.audit-table { min-height: 420px; }
.audit-table :deep(th), .audit-table :deep(td) { font-size: var(--fs-xs); }
.audit-table :deep(th) { white-space: nowrap; }
.endpoint { min-width: 220px; overflow-wrap: anywhere; }
.ip { display: block; margin-top: 2px; color: var(--ink-4); font-family: var(--font-mono); font-size: var(--fs-2xs); }
.port { color: var(--ink-4); font-family: var(--font-mono); }
.nowrap { white-space: nowrap; }
.numeric { text-align: right; }
.blocked-row { background: var(--surface-danger); }
.empty { padding: var(--sp-8) !important; text-align: center; color: var(--ink-4); }
@media (max-width: 720px) {
  .audit-heading { align-items: stretch; }
  .audit-actions { width: 100%; }
  .audit-select { flex: 1 1 130px; }
}
</style>
