<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { api } from '@/composables/useApi'
import { stored } from '@/composables/useStored'
import type { ClusterState, PodLogs } from '@/types'
const state = ref<ClusterState>(), loading = ref(false), error = ref('')
const logs = ref<PodLogs>(), logLoading = ref(false), logError = ref(''), logView = ref<HTMLElement>()
async function refresh() {
  loading.value = true; error.value = ''
  try { state.value = await api<ClusterState>('/api/cluster') }
  catch (e) { error.value = e instanceof Error ? e.message : '状態を取得できません。' }
  finally { loading.value = false }
}
// 止まっているPodは色で分かるようにする。数字だけだと見落とす。
const healthy = (pod: { phase: string; ready: number; containers: number }) =>
  pod.phase === 'Running' && pod.ready === pod.containers

// 畳んだnamespaceをカンマ区切りで覚える。次に開いたときも同じ見え方にする。
const folded = stored('cluster.folded', '')
const foldedSet = computed(() => new Set(folded.value.split(',').filter(Boolean)))
const open = (namespace: string) => !foldedSet.value.has(namespace)
function toggle(namespace: string) {
  const next = new Set(foldedSet.value)
  next.has(namespace) ? next.delete(namespace) : next.add(namespace)
  folded.value = [...next].join(',')
}
// 畳んでいる間も、異常に気づけるようにしておく。
const unhealthy = (section: { pods: { phase: string; ready: number; containers: number }[] }) =>
  section.pods.filter(pod => !healthy(pod)).length
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
    <p class="my-4">管理アプリ・生成・プレビューはKubernetesで動いています。ここは読み取りだけで、操作はしません。</p>
    <v-alert v-if="error" type="warning" density="compact" class="mb-4">{{ error }}</v-alert>
    <p v-if="state && !state.available" class="tip tip--warn">
      クラスタの外で動いています。Pod・Serviceの状態はKubernetes上でのみ確認できます。
    </p>
    <div v-for="section in state?.namespaces ?? []" :key="section.namespace" class="section">
      <button type="button" class="fold" :aria-expanded="open(section.namespace)"
        @click="toggle(section.namespace)">
        <v-icon size="small" :icon="open(section.namespace) ? 'mdi-chevron-down' : 'mdi-chevron-right'" />
        <h3>{{ section.namespace }}</h3>
        <span class="meta">Pod {{ section.pods.length }}</span>
        <span v-if="unhealthy(section)" class="chip chip--sm chip--warn">
          異常 {{ unhealthy(section) }}</span>
      </button>
      <template v-if="open(section.namespace)">
      <v-table density="compact">
        <thead><tr><th>Pod</th><th>状態</th><th>Ready</th><th>再起動</th><th>ノード</th>
          <th>稼働</th><th>イメージ</th><th>ログ</th></tr></thead>
        <tbody>
          <tr v-for="pod in section.pods" :key="pod.name">
            <td>{{ pod.name }}</td>
            <td><span class="chip chip--sm" :class="healthy(pod) ? 'chip--brand' : 'chip--warn'">
              {{ pod.phase }}</span></td>
            <td class="numeric">{{ pod.ready }}/{{ pod.containers }}</td>
            <td class="numeric">{{ pod.restarts }}</td>
            <td>{{ pod.node }}</td>
            <td class="numeric">{{ pod.age }}</td>
            <td class="mono">{{ pod.images.join(' / ') }}</td>
            <td><v-btn variant="text" size="x-small" prepend-icon="mdi-text-box-search-outline"
              @click="showLogs(section.namespace, pod.name)">見る</v-btn></td>
          </tr>
          <tr v-if="!section.pods.length"><td colspan="8" class="meta">Podはありません。</td></tr>
        </tbody>
      </v-table>
      <p class="meta mt-2">
        Deployment：<span v-for="item in section.deployments" :key="item.name" class="deployment">
          {{ item.name }} {{ item.ready }}/{{ item.desired }}
        </span>
        <span v-if="!section.deployments.length">なし</span>
      </p>
      </template>
    </div>
  </v-card>
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
.section { margin-top: var(--sp-6); }
.section h3 { color: var(--ink-1); font-size: var(--fs-sm); margin: 0; }
/* 見出しそのものを開閉の操作にする。行全体を押せるほうが迷わない。 */
.fold { display: flex; align-items: center; gap: var(--sp-2); width: 100%; padding: var(--sp-2) 0;
  background: none; border: 0; cursor: pointer; text-align: left; color: inherit;
  border-bottom: 1px solid var(--border-default); margin-bottom: var(--sp-2); }
.fold:hover h3 { color: var(--text-brand); }
.fold:focus-visible { outline: 2px solid var(--surface-accent-solid); outline-offset: 2px; }
.mono { font-family: var(--font-mono); font-size: var(--fs-xs); }
.numeric { text-align: right; }
.meta { color: var(--ink-4); font-size: var(--fs-xs); }
.deployment { margin-right: var(--sp-3); }
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
