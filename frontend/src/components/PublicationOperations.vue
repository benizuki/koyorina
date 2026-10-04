<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api } from '@/composables/useApi'
import type { PublicationEnvironment, PublicationGrants } from '@/types'
const props = defineProps<{ openProjectId?: string }>()

interface Row { id: string; name: string; tenant_id: string; tenant_name: string
  status: string; build_id: string | null; revision: number | null
  build_created_at: string | null
  candidate_build_id: string | null; candidate_revision: number | null; startable: boolean
  error: string | null; updated_at: string }
interface Pod { name: string; phase: string; ready: number; containers: number; restarts: number
  node: string; reason: string; message: string }
interface Runtime { available: boolean; kind?: 'docker'; pods: Pod[]; deployment: { ready: number; desired: number } | null }
interface Version { id: string; revision: number; created_at: string; digest: string }
interface ResourceValues { cpu_request_m: number; cpu_limit_m: number
  memory_request_mi: number; memory_limit_mi: number; storage_gi: number }
interface ResourceSettings { values: ResourceValues; pvc_size: string | null
  expandable: boolean | null; min_storage_gi: number }
const rows = ref<Row[]>([]), grants = ref<PublicationGrants>(), selected = ref<Row>()
const environment = ref<PublicationEnvironment[]>(), environmentNotice = ref('')
const resources = ref<ResourceSettings>(), resourceNotice = ref(''), resourceError = ref('')
const tenant = ref(''), error = ref(''), busy = ref(false), loading = ref(false), detailsLoading = ref(false), stopping = ref<Row>(), deleting = ref<Row>()
const runtime = ref<Runtime>(), logPod = ref(''), logs = ref<string[]>([]), logBusy = ref(false), logError = ref('')
let logRequest = 0
const versions = ref<Version[]>([]), versionId = ref(''), applying = ref(false)
const versionChoices = computed(() => versions.value.map(item => ({ value: item.id,
  title: `rev${item.revision} · ${new Date(item.created_at).toLocaleString('ja-JP')} · ${item.id.slice(0, 8)}` })))
const tenants = computed(() => [...new Map(rows.value.map(row => [row.tenant_id,
  { title: row.tenant_name, value: row.tenant_id }])).values()])
const visible = computed(() => rows.value.filter(row => !tenant.value || row.tenant_id === tenant.value))
const labels: Record<string, string> = { running: '稼働中', starting: '起動中', updating: '更新中',
  stopped: '停止中', failed: '起動失敗', stopping: '停止中', deleting: '削除中' }
async function refresh() {
  loading.value = true; error.value = ''
  try {
    rows.value = await api<Row[]>('/api/publication-operations')
    if (selected.value) selected.value = rows.value.find(row => row.id === selected.value?.id)
  }
  catch (e) { error.value = e instanceof Error ? e.message : '公開アプリを取得できません。' }
  finally { loading.value = false }
}
async function choose(row: Row) {
  detailsLoading.value = true
  selected.value = row; grants.value = undefined; environment.value = undefined; resources.value = undefined
  runtime.value = undefined; versions.value = []; environmentNotice.value = ''; resourceNotice.value = ''; resourceError.value = ''
  closeLogs(); error.value = ''
  try {
    const resourceResult = api<ResourceSettings>(`/api/publication-operations/${row.id}/resources`)
      .then(value => ({ value, error: '' }), failure => ({ value: undefined,
        error: failure instanceof Error ? failure.message : '実行リソースを取得できません。' }))
    const [allowed, state, builds, savedEnvironment] = await Promise.all([
      api<PublicationGrants>(`/api/publication-operations/${row.id}/grants`),
      api<Runtime>(`/api/publication-operations/${row.id}/runtime`),
      api<Version[]>(`/api/publication-operations/${row.id}/versions`),
      api<{ entries: PublicationEnvironment[] }>(`/api/publication-operations/${row.id}/environment`),
    ])
    const savedResources = await resourceResult
    if (selected.value?.id === row.id) {
      grants.value = allowed; runtime.value = state; versions.value = builds
      environment.value = savedEnvironment.entries
      resources.value = savedResources.value
      resourceError.value = savedResources.error
      versionId.value = row.build_id ?? row.candidate_build_id ?? builds[0]?.id ?? ''
    }
  } catch (e) { error.value = e instanceof Error ? e.message : '運用情報を取得できません。' }
  finally { detailsLoading.value = false }
}
function closeDetails() {
  selected.value = undefined
  grants.value = undefined
  environment.value = undefined
  resources.value = undefined
  environmentNotice.value = ''
  resourceNotice.value = ''
  resourceError.value = ''
  runtime.value = undefined
  closeLogs()
}
async function refreshRuntime() {
  if (!selected.value) return
  try { runtime.value = await api<Runtime>(`/api/publication-operations/${selected.value.id}/runtime`) }
  catch (e) { error.value = e instanceof Error ? e.message : 'Podの状態を取得できません。' }
}
async function readLogs(pod: string) {
  if (!selected.value) return
  const projectId = selected.value.id
  const request = ++logRequest
  logPod.value = pod; logBusy.value = true; logError.value = ''; logs.value = []
  try {
    const result = await api<{ lines: string[] }>(
      `/api/publication-operations/${projectId}/pods/${encodeURIComponent(pod)}/logs`)
    if (request === logRequest) logs.value = result.lines
  } catch (e) {
    if (request === logRequest)
      logError.value = e instanceof Error ? e.message : 'ログを取得できません。'
  } finally {
    if (request === logRequest) logBusy.value = false
  }
}
function closeLogs() {
  logRequest++
  logPod.value = ''; logs.value = []; logError.value = ''; logBusy.value = false
}
async function act(row: Row, action: 'start' | 'stop') {
  busy.value = true; error.value = ''
  try {
    await api(`/api/publication-operations/${row.id}/${action}`, 'POST', {})
    await refresh(); if (selected.value?.id === row.id) await refreshRuntime()
  }
  catch (e) { error.value = e instanceof Error ? e.message : '操作を完了できません。' }
  finally { busy.value = false; stopping.value = undefined }
}
async function deletePublication() {
  if (!deleting.value) return
  busy.value = true; error.value = ''
  try {
    await api(`/api/publication-operations/${deleting.value.id}`, 'DELETE')
    if (selected.value?.id === deleting.value.id) closeDetails()
    deleting.value = undefined
    await refresh()
  } catch (e) { error.value = e instanceof Error ? e.message : '公開アプリを削除できません。' }
  finally { busy.value = false }
}
async function applyVersion() {
  if (!selected.value || !versionId.value) return
  busy.value = true; error.value = ''
  try {
    await api(`/api/projects/${selected.value.id}/publication`, 'POST', { build_id: versionId.value })
    applying.value = false; await refresh(); await refreshRuntime()
  } catch (e) { error.value = e instanceof Error ? e.message : '版を適用できません。' }
  finally { busy.value = false }
}
async function saveGrants() {
  if (!selected.value || !grants.value) return
  busy.value = true; error.value = ''
  try { await api(`/api/publication-operations/${selected.value.id}/grants`, 'PUT', {
    users: grants.value.users, departments: grants.value.departments }) }
  catch (e) { error.value = e instanceof Error ? e.message : '利用許可を保存できません。' }
  finally { busy.value = false }
}
async function saveEnvironment() {
  if (!selected.value || !environment.value) return
  busy.value = true; error.value = ''; environmentNotice.value = ''
  try {
    const saved = await api<{ entries: PublicationEnvironment[] }>(
      `/api/publication-operations/${selected.value.id}/environment`, 'PUT', {
        entries: environment.value.map(entry => ({ ...entry,
          value: entry.secret && entry.configured && entry.value === '' ? null : entry.value })),
      })
    environment.value = saved.entries
    environmentNotice.value = '保存しました。次の起動または版の適用で反映されます。'
  } catch (e) { error.value = e instanceof Error ? e.message : '公開環境変数を保存できません。' }
  finally { busy.value = false }
}
async function saveResources() {
  if (!selected.value || !resources.value) return
  busy.value = true; error.value = ''; resourceNotice.value = ''
  try {
    resources.value = await api<ResourceSettings>(`/api/publication-operations/${selected.value.id}/resources`,
      'PUT', resources.value.values)
    resourceNotice.value = '保存しました。CPU・メモリは次の起動または版の適用で反映されます。PVCの拡張もその際に行います。'
  } catch (e) { error.value = e instanceof Error ? e.message : '実行リソースを保存できません。' }
  finally { busy.value = false }
}
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  void refresh().then(() => {
    const target = rows.value.find(row => row.id === props.openProjectId)
    if (target) void choose(target)
  })
  timer = setInterval(() => {
    if (!loading.value && rows.value.some(row => ['starting', 'updating', 'stopping'].includes(row.status)))
      void refresh().then(refreshRuntime)
  }, 5000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <h1 class="mb-3">公開アプリ運用</h1>
  <p class="tip mb-5">公開アプリの稼働状態、環境変数、利用許可を管理します。停止しても保存データとイメージは保持されます。</p>
  <v-alert v-if="error" type="error" variant="tonal" class="mb-4">{{ error }}</v-alert>
  <div class="d-flex align-center ga-3 mb-4">
    <v-select v-model="tenant" :items="[{ title: 'すべてのテナント', value: '' }, ...tenants]"
      label="テナント" hide-details style="max-width: 22rem" />
    <v-btn variant="outlined" :loading="loading" @click="refresh">状態を更新</v-btn>
  </div>
  <v-card>
    <v-table>
      <thead><tr><th>テナント</th><th>アプリ</th><th>状態</th><th>公開版</th><th>イメージタグ</th><th>ビルド日時</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="row in visible" :key="row.id">
          <td>{{ row.tenant_name }}</td><td>{{ row.name }}</td>
          <td><v-chip :color="row.status === 'running' ? 'success' : row.status === 'failed' ? 'error' : undefined">{{ labels[row.status] ?? row.status }}</v-chip>
            <p v-if="row.error" class="text-error mt-1">{{ row.error }}</p></td>
          <td>{{ row.revision ? `生成版 ${row.revision}` : row.candidate_revision ? `未公開・生成版 ${row.candidate_revision}` : '—' }}</td>
          <td>{{ row.revision ? `rev${row.revision}` : '—' }}</td>
          <td>{{ row.build_created_at ? new Date(row.build_created_at).toLocaleString('ja-JP') : '—' }}</td>
          <td>
            <v-btn size="small" variant="text" :disabled="busy" @click="choose(row)">詳細・設定</v-btn>
            <v-btn v-if="row.startable" size="small" variant="outlined"
              :disabled="busy" @click="act(row, 'start')">起動</v-btn>
            <v-btn v-if="row.status === 'running'" size="small" variant="outlined"
              :disabled="busy" @click="stopping = row">停止</v-btn>
            <v-btn v-if="['stopped', 'deleting'].includes(row.status)" size="small" variant="text" color="error"
              :disabled="busy" @click="deleting = row">削除</v-btn>
          </td>
        </tr>
        <tr v-if="!visible.length"><td colspan="7">管理できる公開アプリはありません。</td></tr>
      </tbody>
    </v-table>
  </v-card>
  <v-dialog :model-value="!!selected" max-width="1100" scrollable
    @update:model-value="value => { if (!value) closeDetails() }">
    <v-card v-if="selected" aria-labelledby="publication-details-title">
      <v-card-title class="d-flex align-center justify-space-between">
        <span id="publication-details-title">{{ selected.name }}の詳細・設定</span>
        <v-btn icon="mdi-close" variant="text" aria-label="閉じる" @click="closeDetails" />
      </v-card-title>
      <v-card-text>
        <v-progress-linear v-if="detailsLoading" indeterminate color="primary" class="mb-4" />
        <v-alert v-if="error" type="error" variant="tonal" class="mb-4">{{ error }}</v-alert>
        <v-card v-if="grants" class="pa-5">
          <h2 class="mb-2">{{ selected.name }}の利用許可</h2>
          <p class="tip mb-4">個人または部門を選びます。テナントの利用者ロールも必要です。</p>
          <div class="grant-columns">
            <v-autocomplete v-model="grants.users" :items="grants.available_users" item-title="name" item-value="id"
              label="人" multiple chips closable-chips :disabled="busy" />
            <v-autocomplete v-model="grants.departments" :items="grants.available_departments" item-title="name" item-value="id"
              label="部門" multiple chips closable-chips :disabled="busy" />
          </div>
          <v-btn class="mt-4" color="primary" :loading="busy" @click="saveGrants">利用許可を保存</v-btn>
        </v-card>
        <v-card class="pa-5 mt-5">
          <h2 class="mb-2">{{ selected.name }}の公開版</h2>
          <p class="tip mb-3">公開タブで登録した版から選びます。過去版への切り替えもできます。更新中は一時停止します。</p>
          <div class="d-flex align-center ga-3">
            <v-select v-model="versionId" :items="versionChoices" label="適用する版" hide-details
              style="max-width: 34rem" :disabled="busy" />
            <v-btn color="primary" :disabled="busy || !versionId || ['starting', 'updating'].includes(selected.status)"
              @click="applying = true">この版を適用</v-btn>
          </div>
        </v-card>
        <v-card v-if="resources" class="pa-5 mt-5">
          <h2 class="mb-2">{{ selected.name }}の実行リソース</h2>
          <p class="tip mb-4">CPU・メモリは次の起動・版の適用で反映します。PVCは縮小できません。
            <span v-if="resources.pvc_size">現在のPVC：{{ resources.pvc_size }}{{ resources.expandable ? '（拡張可能）' : '（拡張不可）' }}</span>
            <span v-else>初回起動時に指定容量のPVCを作成します。最小 {{ Math.max(5, resources.min_storage_gi) }} GiB。</span></p>
          <v-alert v-if="resourceNotice" type="success" variant="tonal" class="mb-4">{{ resourceNotice }}</v-alert>
          <div class="resource-fields mb-4">
            <v-text-field v-model.number="resources.values.cpu_request_m" type="number" min="50" max="4000" step="50"
              label="CPU要求量 (m)" :disabled="busy" />
            <v-text-field v-model.number="resources.values.cpu_limit_m" type="number" min="100" max="8000"
              label="CPU上限 (m)" :disabled="busy" />
            <v-text-field v-model.number="resources.values.memory_request_mi" type="number" min="256" max="16384" step="256"
              label="メモリ要求量 (MiB)" :disabled="busy" />
            <v-text-field v-model.number="resources.values.memory_limit_mi" type="number" min="256" max="32768" step="256"
              label="メモリ上限 (MiB)" :disabled="busy" />
            <v-text-field v-model.number="resources.values.storage_gi" type="number" :min="Math.max(5, resources.min_storage_gi)" max="1020" step="5"
              label="公開データPVC (GiB)" :disabled="busy" />
          </div>
          <v-btn color="primary" :loading="busy" @click="saveResources">実行リソースを保存</v-btn>
        </v-card>
        <v-alert v-else-if="resourceError" type="warning" variant="tonal" class="mt-5">
          実行リソースを取得できません。{{ resourceError }}
        </v-alert>
        <v-card v-if="environment" class="pa-5 mt-5">
          <h2 class="mb-2">{{ selected.name }}の公開環境変数</h2>
          <p class="tip mb-4">プレビューとは別の設定です。秘密の値は表示しません。
            保存した値は次の起動・版の適用で反映されます。稼働中は同じ版を再適用してください。</p>
          <v-alert v-if="environmentNotice" type="success" variant="tonal" class="mb-4">{{ environmentNotice }}</v-alert>
          <v-card v-for="(entry, index) in environment" :key="index" variant="outlined" class="pa-3 mb-3">
            <div class="environment-entry">
              <v-text-field v-model="entry.name" label="名前" :disabled="busy" hide-details />
              <v-text-field v-model="entry.value" :type="entry.secret ? 'password' : 'text'" label="値"
                :placeholder="entry.configured && entry.secret ? '設定済み（空欄で保持）' : ''"
                :disabled="busy" hide-details />
              <v-checkbox v-model="entry.secret" label="秘密として保存" :disabled="busy" hide-details />
              <v-btn variant="text" :disabled="busy" @click="environment.splice(index, 1)">削除</v-btn>
            </div>
          </v-card>
          <div class="d-flex ga-3 flex-wrap">
            <v-btn variant="text" :disabled="busy || environment.length >= 50"
              @click="environment.push({ name: '', value: '', secret: false })">項目を追加</v-btn>
            <v-btn color="primary" :loading="busy" @click="saveEnvironment">環境変数を保存</v-btn>
          </div>
        </v-card>
        <v-card v-if="runtime" class="pa-5 mt-5">
          <div class="d-flex align-center ga-3 mb-3"><h2>{{ selected.name }}の稼働状態</h2>
            <v-btn variant="text" size="small" @click="refreshRuntime">更新</v-btn></div>
          <p v-if="!runtime.available" class="tip">Podの状態はKubernetes環境で確認できます。</p>
          <p v-else class="runtime-summary mb-3">{{ runtime.kind === 'docker' ? 'コンテナ' : 'Deployment' }}：{{ runtime.deployment?.ready ?? 0 }}/{{ runtime.deployment?.desired ?? 0 }} Ready</p>
          <v-table v-if="runtime.available" class="runtime-table"><thead><tr><th>Pod</th><th>状態</th><th>Ready</th><th>再起動</th><th>ノード</th><th>ログ</th></tr></thead>
            <tbody><tr v-for="pod in runtime.pods" :key="pod.name">
              <td>{{ pod.name }}</td><td>{{ pod.reason || pod.phase }}<div v-if="pod.message" class="meta">{{ pod.message }}</div></td>
              <td>{{ pod.ready }}/{{ pod.containers }}</td><td>{{ pod.restarts }}</td><td>{{ pod.node || '—' }}</td>
              <td><v-btn size="small" variant="text" @click="readLogs(pod.name)">ログ</v-btn></td>
            </tr><tr v-if="!runtime.pods.length"><td colspan="6">Podはありません。</td></tr></tbody>
          </v-table>
        </v-card>
      </v-card-text>
      <v-card-actions><v-spacer /><v-btn variant="text" @click="closeDetails">閉じる</v-btn></v-card-actions>
    </v-card>
  </v-dialog>
  <v-dialog :model-value="!!logPod" max-width="1200" scrollable
    @update:model-value="value => { if (!value) closeLogs() }">
    <v-card aria-labelledby="publication-log-title">
      <v-card-title class="d-flex align-center justify-space-between">
        <span id="publication-log-title">{{ selected?.name }}のPodログ</span>
        <v-btn icon="mdi-close" variant="text" aria-label="閉じる" @click="closeLogs" />
      </v-card-title>
      <v-card-text>
        <p class="meta mb-3">{{ logPod }}</p>
        <v-progress-linear v-if="logBusy" indeterminate color="primary" class="mb-3" />
        <v-alert v-if="logError" type="warning" variant="tonal" class="mb-3">{{ logError }}</v-alert>
        <pre class="operation-logs" role="log">{{ logs.join('\n') || (logBusy ? 'ログを取得しています。' : logError ? '' : 'ログはありません。') }}</pre>
      </v-card-text>
      <v-card-actions><v-spacer />
        <v-btn variant="text" :loading="logBusy" @click="readLogs(logPod)">更新</v-btn>
        <v-btn variant="text" @click="closeLogs">閉じる</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
  <v-dialog :model-value="!!stopping" max-width="520" @update:model-value="value => { if (!value) stopping = undefined }">
    <v-card :title="`${stopping?.name ?? ''}を停止しますか？`">
      <v-card-text>利用者はアクセスできなくなります。イメージと保存データは保持します。</v-card-text>
      <v-card-actions><v-btn @click="stopping = undefined">戻る</v-btn>
        <v-btn color="error" :loading="busy" @click="stopping && act(stopping, 'stop')">停止する</v-btn></v-card-actions>
    </v-card>
  </v-dialog>
  <v-dialog :model-value="!!deleting" max-width="520" @update:model-value="value => { if (!value) deleting = undefined }">
    <v-card :title="`${deleting?.name ?? ''}の公開データを削除しますか？`">
      <v-card-text>公開登録と保存データを削除します。このデータは元に戻せません。プロジェクト、ビルド履歴、イメージは残ります。</v-card-text>
      <v-card-actions><v-spacer /><v-btn variant="text" :disabled="busy" @click="deleting = undefined">戻る</v-btn>
        <v-btn color="error" :loading="busy" @click="deletePublication">削除する</v-btn></v-card-actions>
    </v-card>
  </v-dialog>
  <v-dialog v-model="applying" max-width="520"><v-card title="この版を適用しますか？">
    <v-card-text>公開中のアプリは更新中に一時停止します。保存データは保持します。DB構造を変更した版は、別途データ移行が必要です。</v-card-text>
    <v-card-actions><v-btn @click="applying = false">戻る</v-btn>
      <v-btn color="primary" :loading="busy" @click="applyVersion">適用する</v-btn></v-card-actions>
    </v-card>
  </v-dialog>
</template>

<style scoped>
.grant-columns { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--sp-4); }
.resource-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--sp-4); }
.environment-entry { display: grid; grid-template-columns: minmax(10rem, 1fr) minmax(14rem, 2fr) auto auto;
  align-items: center; gap: var(--sp-3); }
.runtime-summary { font-size: var(--fs-sm); }
.runtime-table :deep(th), .runtime-table :deep(td) { font-size: var(--fs-sm); }
.operation-logs { max-height: 60vh; overflow: auto; padding: var(--sp-4); white-space: pre-wrap;
  background: var(--surface-sunken); border: 1px solid var(--border-subtle); border-radius: var(--radius-md);
  font-size: var(--fs-xs); line-height: var(--lh-normal); }
@media (max-width: 700px) {
  .grant-columns, .resource-fields, .environment-entry { grid-template-columns: 1fr; }
}
</style>
