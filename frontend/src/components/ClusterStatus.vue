<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { api } from '@/composables/useApi'
import type { ClusterPodDetails, ClusterState, PodLogs } from '@/types'
const state = ref<ClusterState>(), loading = ref(false), error = ref('')
const logs = ref<PodLogs>(), logLoading = ref(false), logError = ref(''), logView = ref<HTMLElement>()
const namespace = ref('')
const section = computed(() => state.value?.namespaces.find(item => item.namespace === namespace.value))
function namespaceLabel(value: string) {
  const app = state.value?.namespaces[0]?.namespace
  if (value === app) return 'APP'
  return app && value.startsWith(`${app}-`) ? value.slice(app.length + 1).toUpperCase() : value
}
const details = ref<ClusterPodDetails>(), detailLoading = ref(false), detailError = ref('')
async function refresh() {
  loading.value = true; error.value = ''
  try {
    state.value = await api<ClusterState>('/api/cluster')
    if (!state.value.namespaces.some(item => item.namespace === namespace.value))
      namespace.value = state.value.namespaces[0]?.namespace ?? ''
  }
  catch (e) { error.value = e instanceof Error ? e.message : '状態を取得できません。' }
  finally { loading.value = false }
}
// 止まっているPodは色で分かるようにする。数字だけだと見落とす。
const healthy = (pod: { phase: string; ready: number; containers: number }) =>
  pod.phase === 'Running' && pod.ready === pod.containers

const unhealthy = (section: { pods: { phase: string; ready: number; containers: number }[] }) =>
  section.pods.filter(pod => !healthy(pod)).length
async function showDetails(selectedNamespace: string, pod: string) {
  details.value = { namespace: selectedNamespace, pod, conditions: [], events: [] }
  detailLoading.value = true; detailError.value = ''
  try {
    details.value = await api<ClusterPodDetails>(
      `/api/cluster/${encodeURIComponent(selectedNamespace)}/pods/${encodeURIComponent(pod)}/details`)
  } catch (e) { detailError.value = e instanceof Error ? e.message : 'Podの詳細を取得できません。' }
  finally { detailLoading.value = false }
}
async function showLogs(namespace: string, pod: string) {
  logs.value = { namespace, pod, lines: [] }; logLoading.value = true; logError.value = ''
  try {
    logs.value = await api<PodLogs>(`/api/cluster/${encodeURIComponent(namespace)}/pods/${encodeURIComponent(pod)}/logs`)
    await nextTick()
    if (logView.value) logView.value.scrollTop = logView.value.scrollHeight
  } catch (e) { logError.value = e instanceof Error ? e.message : 'ログを取得できません。' }
  finally { logLoading.value = false }
}
function logKind(line: string) {
  const value = line.toLowerCase()
  if (/\b(error|fatal|failed|failure|exception|traceback|panic|denied)\b/.test(value)) return 'error'
  if (/\b(warn|warning|retry|timeout|deprecated)\b/.test(value)) return 'warning'
  if (/\b(success|succeeded|complete|completed|ready|started)\b|\s2\d\d\s/.test(value)) return 'success'
  if (/\b(debug|trace)\b/.test(value)) return 'debug'
  return 'normal'
}
onMounted(refresh)
</script>

<template>
  <v-card class="pa-6 mb-5">
    <div class="page-heading">
      <h2>Kubernetes環境</h2>
      <v-btn variant="text" size="small" prepend-icon="mdi-refresh" :loading="loading"
        @click="refresh">状態を更新</v-btn>
    </div>
    <p class="my-4">管理アプリ・生成・プレビューと、公開基盤のビルド・公開アプリのPod、状態、ログを確認できます。</p>
    <v-alert v-if="error" type="warning" density="compact" class="mb-4">{{ error }}</v-alert>
    <p v-if="state && !state.available" class="tip tip--warn">
      クラスタの外で動いています。Pod・Serviceの状態はKubernetes上でのみ確認できます。
    </p>
    <v-tabs v-if="state?.namespaces.length" v-model="namespace" color="primary" show-arrows class="mb-4">
      <v-tab v-for="item in state.namespaces" :key="item.namespace" :value="item.namespace" :title="item.namespace">
        {{ namespaceLabel(item.namespace) }}
        <span class="meta ml-2">{{ item.pods.length }}</span>
        <span v-if="unhealthy(item)" class="chip chip--sm chip--warn ml-2">要確認 {{ unhealthy(item) }}</span>
      </v-tab>
    </v-tabs>
    <div v-if="section" class="section">
      <v-table density="compact">
        <thead><tr><th>Pod・対象</th><th>状態</th><th>Ready</th><th>再起動</th><th>ノード</th>
          <th>稼働</th><th>イメージ</th><th>確認</th></tr></thead>
        <tbody>
          <tr v-for="pod in section.pods" :key="pod.name">
            <td><span class="mono">{{ pod.name }}</span>
              <div v-if="pod.project_name" class="meta">アプリ：{{ pod.project_name }}</div>
              <div v-if="pod.active_project_names?.length" class="meta">処理中アプリ：{{ pod.active_project_names.join('、') }}</div>
              <div v-if="pod.user_name" class="meta">ユーザー：{{ pod.user_name }}</div></td>
            <td><span class="chip chip--sm" :class="healthy(pod) ? 'chip--brand' : 'chip--warn'">
              {{ pod.reason || pod.phase }}</span>
              <div v-if="pod.message" class="text-caption" style="max-width: 32rem; overflow-wrap: anywhere">{{ pod.message }}</div></td>
            <td class="numeric">{{ pod.ready }}/{{ pod.containers }}</td>
            <td class="numeric">{{ pod.restarts }}</td>
            <td>{{ pod.node }}</td>
            <td class="numeric">{{ pod.age }}</td>
            <td class="mono">{{ pod.images.join(' / ') }}</td>
            <td><v-btn variant="text" size="x-small" @click="showDetails(section.namespace, pod.name)">詳細</v-btn>
              <v-btn variant="text" size="x-small" prepend-icon="mdi-text-box-search-outline"
                @click="showLogs(section.namespace, pod.name)">ログ</v-btn></td>
          </tr>
          <tr v-if="!section.pods.length"><td colspan="8" class="meta">Podはありません。</td></tr>
        </tbody>
      </v-table>
    </div>
  </v-card>
  <v-dialog :model-value="!!details" max-width="1200" scrollable
    @update:model-value="value => { if (!value) details = undefined }">
    <v-card v-if="details">
      <v-card-title class="d-flex align-center justify-space-between">
        <span>{{ details.pod }} の詳細</span>
        <v-btn icon="mdi-close" variant="text" aria-label="閉じる" @click="details = undefined" />
      </v-card-title>
      <v-card-text>
        <p class="meta mb-3">{{ details.namespace }}</p>
        <v-progress-linear v-if="detailLoading" indeterminate color="primary" />
        <v-alert v-if="detailError" type="warning" class="mb-4">{{ detailError }}</v-alert>
        <v-alert v-if="details.status?.reason" type="warning" variant="tonal" class="mb-4">
          <strong>{{ details.status.reason }}</strong>
          <p v-if="details.status.message" class="detail-message">{{ details.status.message }}</p>
        </v-alert>
        <h3 class="mb-2">Podの条件</h3>
        <v-table density="compact"><thead><tr><th>条件</th><th>状態</th><th>理由・内容</th></tr></thead>
          <tbody><tr v-for="condition in details.conditions" :key="condition.type">
            <td>{{ condition.type }}</td><td>{{ condition.status }}</td>
            <td>{{ condition.reason }}<p v-if="condition.message" class="detail-message">{{ condition.message }}</p></td>
          </tr><tr v-if="!details.conditions.length"><td colspan="3">条件はまだありません。</td></tr></tbody>
        </v-table>
        <h3 class="mt-5 mb-2">イベント（新しい順）</h3>
        <v-table density="compact" class="event-table"><thead><tr><th>時刻</th><th>種別・理由</th><th>回数</th><th>内容</th></tr></thead>
          <tbody><tr v-for="(event, index) in details.events" :key="index">
            <td>{{ event.at ? new Date(event.at).toLocaleString('ja-JP') : '—' }}</td>
            <td>{{ event.type }} {{ event.reason }}</td><td>{{ event.count }}</td>
            <td class="detail-message">{{ event.message }}</td>
          </tr><tr v-if="!details.events.length"><td colspan="4">イベントはまだありません。</td></tr></tbody>
        </v-table>
      </v-card-text>
      <v-card-actions><v-spacer /><v-btn variant="text" :loading="detailLoading"
        @click="showDetails(details.namespace, details.pod)">更新</v-btn>
        <v-btn variant="text" @click="details = undefined">閉じる</v-btn></v-card-actions>
    </v-card>
  </v-dialog>
  <v-dialog :model-value="!!logs" max-width="1400" @update:model-value="value => { if (!value) logs = undefined }">
    <v-card class="log-dialog">
      <div class="log-heading">
        <div><p class="meta">{{ logs?.namespace }}</p><h2>{{ logs?.pod }}</h2></div>
        <div class="log-actions">
          <v-btn variant="text" size="small" prepend-icon="mdi-refresh" :loading="logLoading"
            @click="logs && showLogs(logs.namespace, logs.pod)">更新</v-btn>
          <v-btn icon="mdi-close" variant="text" aria-label="閉じる" @click="logs = undefined" />
        </div>
      </div>
      <v-progress-linear v-if="logLoading" indeterminate color="primary" />
      <v-alert v-if="logError" type="warning" density="compact" class="ma-4">{{ logError }}</v-alert>
      <div ref="logView" class="log-view" role="log" aria-live="polite">
        <div v-for="(line, index) in logs?.lines ?? []" :key="index" class="log-line"
          :class="`log-line--${logKind(line)}`"><span class="line-number">{{ index + 1 }}</span><span>{{ line }}</span></div>
        <p v-if="!logLoading && !logError && !logs?.lines.length" class="log-empty">ログはまだありません。</p>
      </div>
      <p class="log-note">直近400行を表示しています。エラー、警告、成功を色分けしています。</p>
    </v-card>
  </v-dialog>
</template>

<style scoped>
.section { overflow-x: auto; }
.detail-message { white-space: pre-wrap; overflow-wrap: anywhere; }
.mono { font-family: var(--font-mono); font-size: var(--fs-xs); }
.numeric { text-align: right; }
.meta { color: var(--ink-4); font-size: var(--fs-xs); }
.event-table :deep(.v-table__wrapper > table > thead > tr > th),
.event-table :deep(.v-table__wrapper > table > tbody > tr > td) { font-size: var(--fs-sm); }
.log-dialog { overflow: hidden; }
.log-heading { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-4); padding: var(--sp-4) var(--sp-5); }
.log-heading h2 { overflow-wrap: anywhere; }
.log-actions { display: flex; align-items: center; gap: var(--sp-2); }
.log-view { height: var(--forge-log-height); overflow: auto; padding: var(--sp-2) 0; background: var(--surface-inverse-deep); color: var(--text-on-inverse); font-family: var(--font-mono); font-size: var(--fs-2xs); line-height: var(--lh-tight); }
.log-line { display: grid; grid-template-columns: var(--sp-10) minmax(0, 1fr); gap: var(--sp-2); padding: var(--sp-1) var(--sp-3); border-left: var(--sp-1) solid transparent; white-space: pre-wrap; overflow-wrap: anywhere; }
.log-line:hover { background: var(--surface-inverse); }
.line-number { color: var(--text-on-inverse-muted); text-align: right; user-select: none; }
.log-line--error { color: var(--text-danger); border-left-color: var(--surface-danger-solid); background: var(--surface-danger); }
.log-line--warning { color: var(--text-warn); border-left-color: var(--surface-warn-solid); background: var(--surface-warn); }
.log-line--success { color: var(--text-brand); border-left-color: var(--surface-accent-solid); }
.log-line--debug { color: var(--text-on-inverse-muted); }
.log-empty { padding: var(--sp-6); text-align: center; color: var(--text-on-inverse-muted); }
.log-note { padding: var(--sp-2) var(--sp-5); color: var(--ink-4); font-size: var(--fs-xs); }
</style>
