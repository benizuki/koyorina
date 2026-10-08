<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useLogLines } from '@/composables/useLogLines'
import { useClipboard } from '@/composables/useClipboard'
import { usePublication } from '@/composables/usePublication'
import type { GenerationJob } from '@/types'
import { generationStamp } from '@/generationLabels'
const props = defineProps<{ projectId: string; jobs: GenerationJob[]; readonly?: boolean; visible?: boolean; canOperate?: boolean }>()
const emit = defineEmits<{
  done: [result: { type: 'success' | 'error'; text: string; label: string }]
  openOperations: []
}>()
const { state, busy, error, logs, refresh, build, release, cancel,
  readLogs, readDockerfile, selectDetails, detailBuildId, dockerfile,
  logsBusy, dockerfileBusy, logsError, dockerfileError } = usePublication(props.projectId)
const { copy } = useClipboard()
const detailTab = ref('logs'), logView = ref<HTMLElement>(), followLogs = ref(true)
const confirmBuild = ref(false), confirmDeploy = ref(false)
const lines = useLogLines(logs)
const imageRepository = computed(() => state.value?.registry_host
  ? `${state.value.registry_host.replace(/\/$/, '')}/${props.projectId}` : '')
const currentRegistryBuilds = computed(() => state.value?.builds.filter(item =>
  item.registry_kind === state.value?.registry_kind &&
  item.image.startsWith(`${imageRepository.value}:`)) ?? [])
const detailBuild = computed(() => currentRegistryBuilds.value.find(item => item.id === detailBuildId.value))
const detailActive = computed(() => !!detailBuild.value && ['queued', 'building', 'pushing'].includes(detailBuild.value.status))
watch(() => currentRegistryBuilds.value[0]?.id, id => { void selectDetails(id) })
watch([logs, detailTab, followLogs], async () => {
  await nextTick()
  if (followLogs.value && detailTab.value === 'logs') logView.value?.scrollTo({ top: logView.value.scrollHeight })
})
const generationId = ref<string>()
const releaseBuildId = ref<string>()
const completedBuilds = computed(() => currentRegistryBuilds.value.filter(item => item.status === 'succeeded' && item.digest))
const releaseChoices = computed(() => completedBuilds.value.map(item => ({ value: item.id,
  title: `rev${item.revision} · ${new Date(item.created_at).toLocaleString('ja-JP')} · ${item.id.slice(0, 8)}` })))
const selectedReleaseBuild = computed(() => completedBuilds.value.find(item => item.id === releaseBuildId.value))
watch(() => completedBuilds.value.map(item => item.id).join(','), () => {
  if (!completedBuilds.value.some(item => item.id === releaseBuildId.value)) releaseBuildId.value = completedBuilds.value[0]?.id
})
const versions = computed(() => props.jobs.filter(j => j.status === 'generated'))
const selectedVersion = computed(() => versions.value.find(job => job.id === generationId.value))
const pickingVersion = ref(false)
watch(versions, jobs => {
  if (!jobs.some(job => job.id === generationId.value)) generationId.value = jobs[0]?.id
}, { immediate: true })
function selectVersion(id: string) {
  generationId.value = id
  pickingVersion.value = false
}
const states: Record<string, string> = { queued: '待機中', building: 'ビルド中', pushing: 'Push中',
  succeeded: '完了', failed: '失敗', cancelled: '中止', pruning: '削除中', stopped: '公開停止', starting: '起動中',
  updating: '更新中', running: '公開中' }
const active = computed(() => state.value?.builds.some(b => ['queued', 'building', 'pushing'].includes(b.status)))
const activeStates = new Set(['queued', 'building', 'pushing'])
const observed = new Map<string, string>()
let initialStateLoaded = false
let buildRequested = false
watch(() => state.value?.builds, builds => {
  if (!builds) return
  if (!initialStateLoaded) {
    for (const item of builds) observed.set(item.id, item.status)
    initialStateLoaded = true
    return
  }
  for (const item of builds) {
    const before = observed.get(item.id)
    const requestedHere = !before && buildRequested
    const completed = !activeStates.has(item.status)
    if (completed && ((before && activeStates.has(before)) || requestedHere)) {
      if (item.status === 'succeeded') emit('done', { type: 'success',
        text: 'ビルド・Pushが完了しました。この版をデプロイできます。', label: '✅ ビルド完了' })
      else if (item.status === 'failed') emit('done', { type: 'error',
        text: 'ビルド・Pushに失敗しました。公開タブでビルドログを確認してください。', label: '⚠️ ビルド失敗' })
      else if (item.status === 'cancelled') emit('done', { type: 'error',
        text: 'ビルドを中止しました。', label: '⚠️ ビルド中止' })
    }
    observed.set(item.id, item.status)
    if (requestedHere) buildRequested = false
  }
})
let timer: ReturnType<typeof setInterval> | undefined
let polling = false, disposed = false
async function poll() {
  if (polling || disposed) return
  polling = true
  const wasActive = detailActive.value
  try {
    await refresh()
    if (!disposed && props.visible && detailTab.value === 'logs' && (wasActive || detailActive.value)) await readLogs()
  } finally { polling = false }
}
onMounted(async () => {
  await refresh()
  if (disposed || !state.value?.enabled) return
  timer = setInterval(poll, 5000)
})
async function startBuild() {
  if (!generationId.value) return
  buildRequested = true
  await build(generationId.value)
  if (error.value) buildRequested = false
  detailTab.value = 'logs'
}
async function confirmStartBuild() {
  confirmBuild.value = false
  await startBuild()
}
async function cancelBuild(id: string) {
  await cancel(id)
  if (detailBuildId.value === id) await readLogs()
}
async function registerRelease() {
  if (!releaseBuildId.value) return
  await release(releaseBuildId.value)
  if (!error.value) emit('done', { type: 'success',
    text: 'デプロイ版を登録しました。公開アプリ運用で環境変数・利用許可を設定し、起動できます。', label: '✅ デプロイ登録' })
}
async function confirmRegisterRelease() {
  confirmDeploy.value = false
  await registerRelease()
}
watch(detailTab, tab => { if (tab === 'logs') void readLogs() })
onUnmounted(() => { disposed = true; if (timer) clearInterval(timer) })
</script>
<template>
  <div>
    <v-alert v-if="error" type="error" variant="tonal" class="mb-3">{{ error }}</v-alert>
    <v-alert v-if="state && !state.enabled" type="info" variant="tonal">公開機能は無効です。管理者に実行基盤の設定を依頼してください。</v-alert>
    <template v-else-if="state">
      <div class="status-row mb-4">
        <v-chip>{{ states[state.status] ?? state.status }}</v-chip>
        <span v-if="state.build_id" class="meta">デプロイ版：{{ state.builds.find(b => b.id === state?.build_id)?.revision ?? state.build_id }}</span>
        <v-btn v-if="canOperate" size="small" variant="text" append-icon="mdi-arrow-right"
          @click="emit('openOperations')">公開アプリ運用</v-btn>
        <v-btn size="small" variant="text" :loading="busy" @click="refresh">状態を更新</v-btn>
      </div>
      <v-alert v-if="state.error" type="warning" variant="tonal" class="mb-3">{{ state.error }}</v-alert>
      <h3 class="mb-3">ビルドする版</h3>
      <div class="version-selection mb-3">
        <span v-if="selectedVersion">選択中：{{ generationStamp(selectedVersion) }}</span>
        <v-btn v-if="versions.length > 1" variant="outlined" :disabled="readonly || busy"
          @click="pickingVersion = !pickingVersion">{{ pickingVersion ? '版の選択を閉じる' : '別の版を選ぶ' }}</v-btn>
        <v-btn :disabled="readonly || busy || active || !generationId || !state?.registry_host" color="primary"
          @click="confirmBuild = true">ビルド・Push</v-btn>
      </div>
      <div v-if="pickingVersion" class="versions mb-3">
        <v-btn v-for="job in versions" :key="job.id" :variant="job.id === generationId ? 'tonal' : 'outlined'"
          :disabled="readonly || busy" :aria-pressed="job.id === generationId"
          :title="`生成ID：${job.id}`" @click="selectVersion(job.id)">{{ generationStamp(job) }}</v-btn>
      </div>
      <p v-if="!versions.length" class="tip mb-3">ビルドできる生成版がありません。生成を完了してください。</p>
      <div class="mt-4">
        <h3 class="mb-3">デプロイ</h3>
        <div class="deploy-selection">
          <v-select v-if="releaseChoices.length" v-model="releaseBuildId" :items="releaseChoices"
            class="release-select" label="ビルド済みの版" hide-details :disabled="readonly || busy" />
          <span v-else class="meta">ビルド済みの版はありません。</span>
          <v-btn color="primary" variant="tonal" :loading="busy"
            :title="state.status !== 'stopped' ? '版を変更するには公開アプリ運用で停止してください。' : undefined"
            :disabled="readonly || busy || !releaseBuildId || state.status !== 'stopped'"
            @click="confirmDeploy = true">デプロイ</v-btn>
        </div>
      </div>
      <p class="meta text-break my-3">保存先：{{ state.registry_kind === 'artifact' ? 'Artifact Registry' : state.registry_kind === 'private' ? '内部Registry' : '未設定' }}{{ imageRepository ? ` · ${imageRepository}` : '' }}</p>
      <p v-if="state.scanning_enabled" class="meta mb-3">保存先で脆弱性検査が有効です。</p>
      <h3 class="mb-3">ビルド履歴</h3>
      <p v-if="!currentRegistryBuilds.length" class="meta mb-3">現在の保存先で作成したビルドはありません。</p>
      <v-list>
        <v-list-item v-for="item in currentRegistryBuilds" :key="item.id" :title="`生成版 ${item.revision} · ${states[item.status]}`" :subtitle="`${new Date(item.created_at).toLocaleString()} · ${item.duration_seconds}秒`">
          <p v-if="item.error">{{ item.error }}</p>
          <p v-if="item.digest" class="text-break">{{ item.image.split(':').slice(0, -1).join(':') }}@{{ item.digest }}</p>
          <v-btn v-if="item.digest" size="small" variant="text" @click="copy('docker pull ' + item.image.slice(0, item.image.lastIndexOf(':')) + '@' + item.digest)">Pullコマンドをコピー</v-btn>
          <v-btn v-if="['queued', 'building', 'pushing'].includes(item.status)" size="small" variant="text" :disabled="readonly || busy" @click="cancelBuild(item.id)">中止</v-btn>
          <span v-if="item.status === 'succeeded'" class="meta">デプロイ候補</span>
        </v-list-item>
      </v-list>
      <v-card variant="outlined" class="pa-4 my-3">
        <h3 class="mb-2">{{ detailBuild ? `直近のビルド：生成版 ${detailBuild.revision} · ${new Date(detailBuild.created_at).toLocaleString('ja-JP')}` : '次のビルド' }}</h3>
        <v-tabs v-model="detailTab" class="mb-3">
          <v-tab value="logs">ビルドログ</v-tab>
          <v-tab value="dockerfile">Dockerfile</v-tab>
        </v-tabs>
        <div v-if="detailTab === 'logs'">
          <div class="detail-actions">
            <span v-if="detailBuild" class="chip chip--sm" :class="detailBuild.status === 'failed' ? 'chip--danger' : 'chip--brand'">{{ states[detailBuild.status] }}</span>
            <v-checkbox v-model="followLogs" label="末尾を追従" hide-details density="compact" />
            <v-btn size="small" variant="text" :loading="logsBusy" :disabled="!detailBuildId" @click="readLogs">ログを更新</v-btn>
            <v-btn size="small" variant="text" :disabled="!logs" prepend-icon="mdi-content-copy" @click="copy(logs)">コピー</v-btn>
          </div>
          <v-alert v-if="logsError" type="warning" class="my-3">{{ logsError }}</v-alert>
          <pre ref="logView" class="build-output" role="log" aria-label="ビルドログ"><span v-for="line in lines" :key="line.key" class="line" :class="`line--${line.kind}`">{{ line.text }}</span><span v-if="!lines.length" class="line">{{ logsBusy ? 'ログを取得しています。' : detailBuildId ? 'まだ出力はありません。ビルド開始後にここへ表示します。' : 'ビルドを開始するとここにログが表示されます。' }}</span></pre>
          <p class="meta mt-2">{{ detailActive ? '実行中は5秒ごとに自動更新します。' : '直近のビルドのログを表示しています。' }}直近300行を表示し、エラー・注意を色分けします。</p>
        </div>
        <div v-else>
          <div class="detail-actions">
            <span class="meta">{{ detailBuildId ? '直近のビルドで使用した管理Dockerfile' : '次のビルドで使用する管理Dockerfile' }}</span>
            <v-btn size="small" variant="text" :loading="dockerfileBusy" @click="readDockerfile">更新</v-btn>
            <v-btn size="small" variant="text" :disabled="!dockerfile?.dockerfile" prepend-icon="mdi-content-copy" @click="copy(dockerfile?.dockerfile ?? '')">コピー</v-btn>
          </div>
          <v-alert v-if="dockerfileError" type="warning" class="my-3">{{ dockerfileError }}</v-alert>
          <pre class="build-output" aria-label="Dockerfile">{{ dockerfileBusy ? 'Dockerfileを取得しています。' : dockerfile?.dockerfile || (detailBuildId ? 'このビルドのDockerfileは保存されていません。' : 'Dockerfileはまだ取得できません。') }}</pre>
          <p class="meta mt-2">VueのフロントエンドとFastAPIのバックエンドをビルドします。このDockerfileは読み取り専用です。</p>
        </div>
      </v-card>
      <h3 class="mb-3">デプロイ履歴</h3>
      <v-list class="deploy-history"><v-list-item v-for="(event, index) in state.history" :key="index" :title="event.action === 'stop' ? '停止' : event.action === 'release' ? 'デプロイ版を登録' : '版の適用'" :subtitle="`${new Date(event.at).toLocaleString()} · ${event.build_id ?? ''}`" /></v-list>
    </template>
    <v-dialog v-model="confirmBuild" max-width="520">
      <v-card title="ビルド・Pushしますか？">
        <v-card-text>選択した{{ selectedVersion ? generationStamp(selectedVersion) : '生成版' }}をビルドし、RegistryへPushします。ビルド完了だけではデプロイ版は変わりません。</v-card-text>
        <v-card-actions><v-spacer /><v-btn variant="text" @click="confirmBuild = false">戻る</v-btn>
          <v-btn color="primary" :disabled="readonly || busy || !generationId" @click="confirmStartBuild">ビルド・Push</v-btn></v-card-actions>
      </v-card>
    </v-dialog>
    <v-dialog v-model="confirmDeploy" max-width="520">
      <v-card title="この版をデプロイしますか？">
        <v-card-text>rev{{ selectedReleaseBuild?.revision ?? '—' }} を運用対象に登録します。登録だけでは起動しません。起動と利用許可の設定は「公開アプリ運用」で行います。</v-card-text>
        <v-card-actions><v-spacer /><v-btn variant="text" @click="confirmDeploy = false">戻る</v-btn>
          <v-btn color="primary" :disabled="readonly || busy || !releaseBuildId || state?.status !== 'stopped'"
            @click="confirmRegisterRelease">デプロイ</v-btn></v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<style scoped>
.status-row, .deploy-selection { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }
.release-select { flex: 1 1 20rem; max-width: 34rem; }
.version-selection, .versions { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }
.detail-actions { display: flex; align-items: center; flex-wrap: wrap; gap: var(--sp-2); margin-bottom: var(--sp-2); }
.detail-actions .v-checkbox { flex: 0 1 auto; }
.build-output { height: var(--forge-log-height); overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere;
  padding: var(--sp-4); border: 1px solid var(--border-subtle); border-radius: var(--radius-md);
  background: var(--surface-sunken); color: var(--ink-2); font-family: var(--font-mono); font-size: var(--fs-xs); }
.line { display: block; }
.line:empty::before { content: "\00a0"; }
.line--error { color: var(--text-danger); box-shadow: inset var(--sp-1) 0 var(--text-danger); padding-left: var(--sp-2); }
.line--warn { color: var(--text-warn); }
.line--ready { color: var(--text-brand); }
.line--info { color: var(--ink-3); }
.deploy-history :deep(.v-list-item-title) { font-size: var(--fs-sm); }
.deploy-history :deep(.v-list-item-subtitle) { font-size: var(--fs-xs); }
</style>
