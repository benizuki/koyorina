<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import ThemeToggle from '@/components/ThemeToggle.vue'
import PdfFieldImport from '@/components/PdfFieldImport.vue'
import ProjectCreationProfile from '@/components/ProjectCreationProfile.vue'
import PromptProjectCreate from '@/components/PromptProjectCreate.vue'
import ReferenceImageInput from '@/components/ReferenceImageInput.vue'
import SampleDataImport from '@/components/SampleDataImport.vue'
import SpreadsheetFieldImport from '@/components/SpreadsheetFieldImport.vue'
import ClusterStatus from '@/components/ClusterStatus.vue'
import NetworkAudit from '@/components/NetworkAudit.vue'
import CodexConnection from '@/components/CodexConnection.vue'
import MasterAdmin from '@/components/MasterAdmin.vue'
import TenantAdmin from '@/components/TenantAdmin.vue'
import DevelopWorkspace from '@/components/DevelopWorkspace.vue'
import ProjectReadOnly from '@/components/ProjectReadOnly.vue'
import ProjectSharing from '@/components/ProjectSharing.vue'
import RequirementsInterview from '@/components/RequirementsInterview.vue'
import LoginPanel from '@/components/LoginPanel.vue'
import KoyoriLogo from '@/components/KoyoriLogo.vue'
import { APP_NAME } from '@/branding'
import { api } from '@/composables/useApi'
import { useAuth } from '@/composables/useAuth'
import { uploadAttachment } from '@/composables/useAttachments'
import { useProjects } from '@/composables/useProjects'
import { useTenantFilter } from '@/composables/useTenantFilter'
import type { CreationProfile, FieldSpec, GeneratorRuntimeState, GeneratorRuntimes, Project, ProjectInput, PurposeDraft, TableSpec, Tenant } from '@/types'
const auth = useAuth()
const { user, config, loaded, error: authError } = auth
const { projects, loading, error, perform, refresh, save, approve, remove } = useProjects()
const deleting = ref<Project>(), confirmName = ref(''), removalNotice = ref('')
function askDelete(project: Project) { deleting.value = project; confirmName.value = '' }
async function confirmDelete() {
  const target = deleting.value
  if (!target || confirmName.value.trim() !== target.name) return
  // 完了するまで閉じない。先に閉じると、消えていく表示に undefined が出る。
  const result = await remove(target)
  deleting.value = undefined
  if (result) {
    if (selected.value?.id === target.id) selected.value = undefined
    removalNotice.value = result.remaining.length
      ? `「${target.name}」を削除しました。片付けられなかったものがあります：${result.remaining.join('、')}。管理者に確認してください。`
      : `「${target.name}」を削除しました。`
  }
}
const tab = ref('projects'), search = ref(''), step = ref(0)
// 幅を使い切るのは、横に並べる開発画面と、表が主役のクラスタ状態。
// 文章が主体の画面まで広げると、1行が長くなって読みにくい。
const editing = ref<Project>(), selected = ref<Project>()
// プロンプトから作る画面。修正するときは対象のアプリを持つ。
const prompting = ref(false), promptProject = ref<Project>()
const wide = computed(() =>
  (tab.value === 'projects' && !!selected.value && !step.value) ||
  tab.value === 'connections' || tab.value === 'network-audit')
// 一覧が主体の画面は、文章主体の幅では列が詰まる。画面幅に追従させる。
const broad = computed(() => tab.value === 'masters')
const name = ref(''), purpose = ref('')
const purposeDrafting = ref(false), purposeDraftError = ref('')
const blankProfile = (): CreationProfile => ({ app_pattern: 'data_management', work_category: 'indirect', app_type: 'records', goals: [],
  other_goal: '', production_day_start: null, production_day_hours: null, default_period: null,
  sample_data: null, column_mappings: [] })
function copyProfile(profile?: CreationProfile | null): CreationProfile {
  const defaults = blankProfile()
  if (!profile) return defaults
  const legacyPatterns: Partial<Record<CreationProfile['app_type'], CreationProfile['app_pattern']>> = {
    visualization: 'local_file_visualization', both: 'file_import',
  }
  const appPattern = profile.app_pattern ?? legacyPatterns[profile.app_type] ?? 'data_management'
  return {
    app_pattern: appPattern,
    work_category: profile.work_category ?? defaults.work_category,
    app_type: profile.app_type ?? defaults.app_type,
    goals: [...(profile.goals ?? [])],
    other_goal: profile.other_goal ?? '',
    production_day_start: profile.production_day_start ?? null,
    production_day_hours: profile.production_day_hours ?? null,
    default_period: profile.default_period ?? null,
    sample_data: profile.sample_data
      ? {
          ...profile.sample_data,
          columns: (profile.sample_data.columns ?? []).map(column => ({
            ...column,
            samples: [...(column.samples ?? [])],
          })),
          warnings: [...(profile.sample_data.warnings ?? [])],
        }
      : null,
    column_mappings: (profile.column_mappings ?? []).map(mapping => ({ ...mapping })),
  }
}
const creationProfile = ref<CreationProfile>(blankProfile())
const referenceImages = ref<File[]>([])
// アプリを置くテナント。選べるのは、そこでアプリを作れる（開発者・管理者の）ものだけ。
const tenants = ref<Tenant[]>([])
const tenantId = ref<string>()
const tenantChoices = computed(() => tenants.value
  .filter(t => t.enabled && (user.value?.develop_tenant_ids ?? []).includes(t.id))
  .map(t => ({ title: t.name, value: t.id })))
const blankFields = (): FieldSpec[] => [{ name: '', kind: 'text', required: true }]
const tables = ref<TableSpec[]>([{ name: '記録', kind: 'record', fields: blankFields() }])
const tableTab = ref(0)
const fields = computed(() => tables.value[tableTab.value]?.fields ?? [])
const consent = ref(false), googleButton = ref<HTMLElement>()
const runtimes = ref<GeneratorRuntimes>()
let runtimeTimer: ReturnType<typeof setTimeout> | undefined
let workspaceRetryTimer: ReturnType<typeof setTimeout> | undefined
const warmedTenants = new Set<string>()
const runtimeView: Record<GeneratorRuntimeState, { label: string; icon: string; color?: string; help: string }> = {
  running: { label: 'Run', icon: 'mdi-play-circle', color: 'success', help: '生成Podは起動中です' },
  starting: { label: 'Start', icon: 'mdi-progress-clock', color: 'warning', help: '生成Podの起動を待っています' },
  stopped: { label: 'Stop', icon: 'mdi-stop-circle-outline', help: '生成Podは停止しています' },
  error: { label: 'Error', icon: 'mdi-alert-circle', color: 'error', help: '生成Podを起動できていません' },
  unavailable: { label: 'Off', icon: 'mdi-minus-circle-outline', help: 'このAIは利用できません' },
}
async function refreshRuntimes() {
  clearTimeout(runtimeTimer)
  if (!user.value) { runtimes.value = undefined; return }
  try { runtimes.value = await api<GeneratorRuntimes>('/api/codex/runtimes') } catch { /* 次の周期で再確認する */ }
  runtimeTimer = setTimeout(refreshRuntimes, 5000)
}
async function warmRuntime(selectedTenant?: string) {
  if (!selectedTenant || warmedTenants.has(selectedTenant)) return
  warmedTenants.add(selectedTenant)
  try {
    await api(`/api/codex/runtimes/${selectedTenant}/start`, 'POST', {})
    await refreshRuntimes()
  } catch {
    // A failed warm-up is retried when the tenant is selected again or an AI action starts.
    warmedTenants.delete(selectedTenant)
  }
}
async function loadTenantsAndWarm() {
  try { tenants.value = await api<Tenant[]>('/api/tenants') } catch { tenants.value = [] }
  tenantId.value = tenantChoices.value[0]?.value
  await warmRuntime(tenantId.value)
}
async function retryWorkspace() {
  clearTimeout(workspaceRetryTimer)
  authError.value = ''
  await perform(refresh)
  if (!error.value) await loadTenantsAndWarm()
}
function scheduleWorkspaceRetry() {
  clearTimeout(workspaceRetryTimer)
  workspaceRetryTimer = setTimeout(() => { if (user.value && error.value) void retryWorkspace() }, 5000)
}
type Provider = 'codex' | 'gemini' | 'antigravity' | 'openai_compatible' | 'claude'
const runtimeTitle = (name: Provider) => {
  const value = runtimes.value?.[name]
  if (!value) return `${name === 'codex' ? 'Codex' : 'Gemini'}：状態を確認中です`
  // 所属テナントが無いと起動要求そのものを送れず、原因が分からず「停止」に見える。
  if (value.state === 'stopped' && tenantChoices.value.length === 0)
    return '所属するテナントがないため生成環境を起動できません。管理者に確認してください。'
  const detail = value.pods ? `（Pod ${value.pods}、起動中 ${value.running}、起動待ち ${value.starting}）` : ''
  return `${runtimeView[value.state].help}${detail}`
}
const runtimeLabel = (name: Provider) =>
  name === 'codex' ? 'Codex' : name === 'gemini' ? 'Gemini' : name === 'antigravity' ? 'Antigravity'
    : name === 'claude' ? 'Claude' : (config.value?.openai_compatible_label || 'OpenAI互換API')
const noTenant = (name: Provider) =>
  runtimes.value?.[name]?.state === 'stopped' && tenantChoices.value.length === 0
const runtimeChipColor = (name: Provider) =>
  noTenant(name) ? 'error' : (runtimes.value ? runtimeView[runtimes.value[name].state].color : undefined)
const runtimeChipIcon = (name: Provider) =>
  noTenant(name) ? 'mdi-account-off-outline' : (runtimes.value ? runtimeView[runtimes.value[name].state].icon : 'mdi-dots-horizontal-circle-outline')
const runtimeChipLabel = (name: Provider) =>
  noTenant(name) ? '所属なし' : (runtimes.value ? runtimeView[runtimes.value[name].state].label : '...')
const runtimeProviders = computed(() => (['codex', 'gemini', 'antigravity', 'openai_compatible', 'claude'] as const)
  .filter(provider => {
    if (provider === 'codex') return config.value?.codex_available !== false && user.value?.can_use_codex === true
    if (provider === 'gemini') return config.value?.gemini_available === true
    if (provider === 'antigravity') return config.value?.antigravity_available === true
    if (provider === 'claude') return config.value?.claude_available === true
    return config.value?.openai_compatible_available === true
  }))
// テナントの絞り込み。選択肢はアプリケーションバーに置く。
const filterTenantIds = computed(() => new Set(user.value?.can_manage_users
  ? tenants.value.map(tenant => tenant.id)
  : [...(user.value?.tenant_ids ?? []), ...(user.value?.admin_tenant_ids ?? [])]))
const { selected: filterTenant, choices: filterChoices, offered: showTenantFilter,
        matches: inTenant, nameOf: tenantName } = useTenantFilter(projects, tenants, filterTenantIds)
watch(filterTenant, value => {
  if (value) void warmRuntime(value)
})
const filtered = computed(() => projects.value.filter(p =>
  inTenant(p)
  && `${p.name} ${p.purpose} ${p.owner_name ?? ''} ${p.owner_email ?? ''}`.includes(search.value || '')))
// 絞った結果が空のとき、原因が検索語なのかテナントなのかを言い分ける。
const narrowed = computed(() => !!search.value || filterTenant.value !== '')
const validFields = computed(() => tables.value.length > 0 && tables.value.some(t => t.kind === 'record') &&
  tables.value.every(t => t.name.trim() && t.fields.length > 0 && t.fields.every(f => f.name.trim()) &&
    new Set(t.fields.map(f => f.name.trim().toLowerCase())).size === t.fields.length) &&
  new Set(tables.value.map(t => t.name.trim().toLowerCase())).size === tables.value.length)
const validProfile = computed(() => creationProfile.value.goals.length > 0
  && (!creationProfile.value.goals.includes('other') || creationProfile.value.other_goal.trim().length >= 3))
const purposeSaved = computed(() => !!selected.value && purpose.value.trim() === selected.value.purpose)
const kindOptions = [{ title: '短い文字', value: 'text' }, { title: '長い文章', value: 'longtext' }, { title: '数値', value: 'number' }, { title: '日付', value: 'date' }, { title: 'はい／いいえ', value: 'bool' }]
function start(project?: Project) {
  // Vueのreactive ProxyはstructuredCloneできない。画面を閉じる前に編集用の値を作る。
  const nextProfile = copyProfile(project?.creation_profile)
  const nextTables: TableSpec[] = project
    ? project.tables.map(t => ({ ...t, fields: t.fields.map(f => ({ ...f })) }))
    : [{ name: '記録', kind: 'record', fields: blankFields() }]

  editing.value = project
  name.value = project?.name ?? ''; purpose.value = project?.purpose ?? ''
  purposeDraftError.value = ''
  creationProfile.value = nextProfile
  referenceImages.value = []
  tables.value = nextTables
  tableTab.value = 0
  step.value = 1; tab.value = 'projects'; error.value = ''
  selected.value = undefined
}
function editSelected() {
  const project = selected.value
  if (!project) return
  // 依頼文だけで作ったアプリは帳票を持たない。手順の画面で開くと空の帳票から始まってしまう。
  if (project.creation_profile?.mode === 'prompt') startPrompt(project)
  else start(project)
}
function startPrompt(project?: Project) {
  promptProject.value = project; prompting.value = true
  step.value = 0; tab.value = 'projects'; error.value = ''
  selected.value = undefined
}
function cancelPrompt() {
  selected.value = promptProject.value
  prompting.value = false; promptProject.value = undefined
}
async function submitPrompt(input: ProjectInput) {
  const project = await save(input, promptProject.value, tenantId.value)
  if (!project) return
  prompting.value = false; promptProject.value = undefined
  editing.value = undefined; selected.value = project; consent.value = false
}
function applySampleFields(value: FieldSpec[]) {
  if (!value.length) return
  const target = tables.value.findIndex(table => table.kind === 'record')
  const index = target >= 0 ? target : 0
  tables.value[index] = { ...tables.value[index], name: '取込データ', kind: 'record', fields: value }
  tableTab.value = index
}
function cleanTables() {
  return tables.value.map(t => ({ ...t, name: t.name.trim(),
    fields: t.fields.map(f => ({ ...f, name: f.name.trim() })) }))
}
function fallbackPurpose() {
  const action = creationProfile.value.app_pattern === 'local_file_visualization'
    ? '製造データの推移をグラフで確認し、日々の状況を把握する'
    : creationProfile.value.app_pattern === 'data_management'
      ? '業務データを登録・共有し、対応状況を分かりやすく管理する'
      : '取り込んだデータを蓄積し、グラフや集計結果を確認する'
  return `${name.value.trim()}で${action}ために使います。`
}
async function generatePurposeDraft() {
  purposeDrafting.value = true; purposeDraftError.value = ''
  try {
    const result = await api<PurposeDraft>('/api/projects/purpose-draft', 'POST', {
      name: name.value.trim(), creation_profile: creationProfile.value, tables: cleanTables(),
    })
    purpose.value = result.purpose
  } catch (e) {
    if (!purpose.value.trim()) purpose.value = fallbackPurpose()
    purposeDraftError.value = e instanceof Error ? e.message : 'AIで目的の下書きを作れませんでした。'
  } finally { purposeDrafting.value = false }
}
async function preparePurpose() {
  if (!validFields.value) return
  tables.value = cleanTables()
  if (!editing.value || !purpose.value.trim()) await generatePurposeDraft()
  step.value = 3
}
async function persistForInterview() {
  if (!validFields.value || purpose.value.trim().length < 5) return
  const cleaned = cleanTables()
  const primary = cleaned.find(t => t.kind === 'record')!
  const input: ProjectInput = { name: name.value.trim(), purpose: purpose.value.trim(),
    audience: editing.value?.audience ?? 'team', fields: primary.fields, tables: cleaned,
    requirements: editing.value?.requirements ?? [],
    generation_prompt: editing.value?.generation_prompt ?? '', creation_profile: creationProfile.value }
  const project = await save(input, editing.value, tenantId.value)
  if (project) {
    editing.value = project; purpose.value = project.purpose
    step.value = 3; consent.value = false
    try {
      while (referenceImages.value.length) {
        await uploadAttachment(project.id, referenceImages.value[0])
        referenceImages.value = referenceImages.value.slice(1)
      }
    } catch (e) {
      error.value = e instanceof Error ? `参考画面を添付できませんでした。${e.message}` : '参考画面を添付できませんでした。'
      return
    }
    selected.value = project
  }
}
function open(project: Project) { selected.value = project; step.value = 0; consent.value = false }
// 共有・テナントの変更を、いま開いている画面へすぐ反映する。
// 一覧の取り直しだけだと、開いているアプリの情報が古いまま残る。
async function applyProjectChange(project: Project) {
  selected.value = project
  if (editing.value?.id === project.id) editing.value = project
  await refresh()
}
function resumeInterview() {
  const project = selected.value
  if (!project) return
  // 仕様画面から戻るときも、名前・目的・帳票・作成条件をフォームへ読み込む。
  // 画面を切り替えるだけだと目的が空のまま表示され、保存済みの判定に通らず
  // ヒアリングの欄そのものが出なかった。
  start(project)
  selected.value = project; step.value = 3; consent.value = false
}
function openSpecification() {
  // ヒアリングを終えたら仕様画面へ渡す。確定はそこで一度だけ行う。
  editing.value = undefined
  step.value = 0
  consent.value = false
}
async function confirm() {
  if (selected.value && consent.value) {
    const p = await approve(selected.value)
    if (p) { selected.value = p; editing.value = undefined; step.value = 0 }
  }
}
async function interviewApplied(project: Project) {
  selected.value = project; editing.value = project; purpose.value = project.purpose
  consent.value = false; await refresh()
}
async function updateRequirements(requirements: string[]) {
  if (!selected.value) return
  const current = selected.value
  const updated = await save({ name: current.name, purpose: current.purpose, audience: current.audience,
    fields: current.fields, tables: current.tables, requirements,
    generation_prompt: current.generation_prompt,
    creation_profile: current.creation_profile }, current)
  if (updated) { selected.value = updated; consent.value = false }
}
async function updateGenerationPrompt(generationPrompt: string) {
  if (!selected.value) return
  const current = selected.value
  const updated = await save({ name: current.name, purpose: current.purpose, audience: current.audience,
    fields: current.fields, tables: current.tables, requirements: current.requirements,
    generation_prompt: generationPrompt, creation_profile: current.creation_profile }, current)
  if (updated) { selected.value = updated; consent.value = false }
}
async function mountLogin() { await nextTick(); if (googleButton.value) await auth.mountGoogle(googleButton.value).catch((e: Error) => { authError.value = e.message }) }
watch(user, async value => {
  if (value) {
    await perform(refresh)
    if (error.value) scheduleWorkspaceRetry()
    await loadTenantsAndWarm()
    await refreshRuntimes()
  } else {
    clearTimeout(runtimeTimer); clearTimeout(workspaceRetryTimer)
    runtimes.value = undefined; warmedTenants.clear()
    if (loaded.value) await mountLogin()
  }
})
// マスター画面でテナントや自分の所属が変わったら、戻る際に権限と一覧を取り直す。
watch(tab, value => { if (value === 'projects' && user.value) void auth.fetchMe() })
onUnmounted(() => { clearTimeout(runtimeTimer); clearTimeout(workspaceRetryTimer) })
onMounted(async () => {
  await auth.fetchMe()
  if (!user.value) return mountLogin()
})
</script>

<template>
  <v-app-bar flat border="b">
    <v-app-bar-title class="brand">
      <KoyoriLogo :size="24" /><strong class="app-name">{{ APP_NAME }}</strong>
    </v-app-bar-title>
    <v-tabs v-if="user" v-model="tab" color="primary" density="comfortable" class="bar-tabs">
      <v-tab value="projects">プロジェクト</v-tab>
      <v-tab value="connections">接続・公開環境</v-tab>
      <v-tab v-if="user.can_manage_users" value="network-audit">通信ログ</v-tab>
      <v-tab v-if="user.can_manage_users" value="masters">マスター管理</v-tab>
      <!-- システム管理者はマスター管理のテナントから開ける。テナント管理者にはこちらを出す。 -->
      <v-tab v-if="!user.can_manage_users && user.admin_tenant_ids?.length" value="tenant-admin">テナント設定</v-tab>
    </v-tabs>
    <v-spacer />
    <div v-if="user" class="runtime-status" aria-label="生成AIの実行状態">
      <v-tooltip v-for="provider in runtimeProviders" :key="provider"
        :text="runtimeTitle(provider)" location="bottom">
        <template #activator="{ props: tooltipProps }">
          <v-chip v-bind="tooltipProps" size="small" variant="tonal"
            :color="runtimeChipColor(provider)"
            :class="{ 'runtime-chip--starting': runtimes?.[provider].state === 'starting' }">
            <v-icon start :icon="runtimeChipIcon(provider)" />
            {{ runtimeLabel(provider) }}
            <strong>{{ runtimeChipLabel(provider) }}</strong>
          </v-chip>
        </template>
      </v-tooltip>
    </div>
    <!-- 新しいテナントも、最初のアプリを作る前から確認できる。 -->
    <v-select v-if="user && tab === 'projects' && showTenantFilter"
      v-model="filterTenant" :items="filterChoices"
      density="compact" variant="outlined" hide-details class="bar-filter"
      prepend-inner-icon="mdi-filter-variant" aria-label="テナントで絞り込む" />
    <ThemeToggle />
    <v-btn v-if="user && config?.auth_mode === 'google'" variant="text" @click="auth.logout">ログアウト</v-btn>
  </v-app-bar>
  <v-main>
    <v-container class="forge-container"
      :class="{ 'forge-container--wide': wide, 'forge-container--broad': broad,
        'forge-container--workspace': tab === 'projects' && !!selected && !step }">
      <p v-if="config?.auth_mode === 'dev-bypass'" class="tip tip--warn mb-5">ローカル検証：Googleログインを省略しています。この状態では外部公開できません。</p>
      <v-progress-linear v-if="!loaded || loading" indeterminate color="primary" />
      <v-alert v-if="authError || error" type="error" class="mb-5" closable
        @click:close="authError = ''; error = ''">
        <div class="alert-content"><span>{{ authError || error }}</span>
          <v-btn v-if="user" variant="text" size="small" @click="retryWorkspace">再読み込み</v-btn></div>
      </v-alert>
      <LoginPanel v-if="loaded && !user" :configured="!!config?.google_client_id">
        <div ref="googleButton" />
      </LoginPanel>
      <template v-if="user">
        <template v-if="tab === 'projects'">
          <template v-if="!step && !selected && !prompting">
            <div class="page-heading">
              <div><p class="eyebrow">YOUR WORKSPACE</p><h1>つくりたいを、かたちに。</h1><p class="subheading">質問に答えて仕様を確認。納得してから開発を始めましょう。</p></div>
              <div class="heading-actions">
                <v-btn variant="outlined" prepend-icon="mdi-text-box-edit-outline" @click="startPrompt()">プロンプトから作る</v-btn>
                <v-btn color="primary" prepend-icon="mdi-plus" @click="start()">新しいアプリを作る</v-btn>
              </div>
            </div>
            <div class="summary-grid my-6">
              <v-card class="pa-5"><p>プロジェクト</p><strong class="summary-number">{{ projects.length }}</strong></v-card>
              <v-card class="pa-5"><p>仕様の確認中</p><strong class="summary-number">{{ projects.filter(p => p.status === 'draft').length }}</strong></v-card>
              <v-card class="pa-5"><p>仕様の承認済み</p><strong class="summary-number">{{ projects.filter(p => p.status === 'approved').length }}</strong></v-card>
            </div>
            <v-text-field v-model="search" label="プロジェクトを検索" prepend-inner-icon="mdi-magnify" clearable class="mb-5" />
            <v-alert v-if="removalNotice" type="info" variant="tonal" closable class="my-4"
              @click:close="removalNotice = ''">{{ removalNotice }}</v-alert>
            <v-card v-if="!filtered.length" class="pa-8 text-center">
              <v-icon icon="mdi-folder-plus-outline" size="x-large" color="primary" class="mb-4" />
              <h2>{{ narrowed ? '該当するプロジェクトがありません' : '最初のアプリを考えてみましょう' }}</h2>
              <p v-if="narrowed" class="my-4">
                絞り込みを外すと、他のテナントのアプリも出ます。</p>
              <p v-else class="my-4">備品の管理、顧客の台帳、問い合わせの記録などから始められます。</p>
              <div v-if="!narrowed" class="empty-actions">
                <v-btn variant="outlined" @click="start()">質問に答えて始める</v-btn>
                <v-btn variant="text" prepend-icon="mdi-text-box-edit-outline" @click="startPrompt()">プロンプトから作る</v-btn>
              </div>
            </v-card>
            <div v-else class="project-grid">
              <v-card v-for="project in filtered" :key="project.id" class="pa-5">
                <span class="chip chip--pill" :class="project.status === 'approved' ? 'chip--brand' : 'chip--warn'">{{ project.status === 'approved' ? '仕様承認済み' : '仕様確認中' }}</span>
                <h2 class="mt-4">{{ project.name }}</h2><p class="my-3">{{ project.purpose }}</p>
                <span v-if="project.is_owner === false" class="chip chip--sm ml-2">共有されたアプリ</span>
                <span v-if="tenantName(project.tenant_id)" class="chip chip--sm ml-2">
                  {{ tenantName(project.tenant_id) }}</span>
                <p v-if="user.can_manage_users || project.is_owner === false" class="meta">
                  作成者：{{ project.owner_name || project.owner_id }}</p>
                <p v-else-if="project.collaborator_count" class="meta">
                  共同開発者：{{ project.collaborator_count }}人</p>
                <p class="meta mb-4">更新：{{ new Date(project.updated_at).toLocaleString('ja-JP') }}</p>
                <div class="card-actions">
                  <v-btn variant="outlined" @click="open(project)">仕様を見る</v-btn>
                  <v-btn v-if="project.can_administer" variant="outlined" color="error"
                    :disabled="loading" @click="askDelete(project)">削除</v-btn>
                </div>
              </v-card>
            </div>
          </template>
          <template v-else-if="prompting">
            <v-btn variant="text" prepend-icon="mdi-arrow-left" @click="cancelPrompt">
              {{ promptProject ? '仕様画面に戻る' : '一覧に戻る' }}</v-btn>
            <PromptProjectCreate :key="promptProject?.id ?? 'new'" v-model:tenant-id="tenantId"
              :project="promptProject" :loading="loading" :tenant-choices="tenantChoices"
              @submit="submitPrompt" @cancel="cancelPrompt" />
          </template>
          <template v-else-if="step">
            <v-btn variant="text" prepend-icon="mdi-arrow-left" @click="step = 0">一覧に戻る</v-btn>
            <v-card class="pa-6 mt-4">
              <p class="eyebrow">{{ step }} / 3　{{ step === 1 ? '仕様確認' : step === 2 ? 'データ確認' : '目的・ヒアリング' }}</p>
              <v-progress-linear :model-value="step / 3 * 100" color="primary" class="my-5" />
              <div v-if="step === 1" class="form-stack">
                <h1>どんな作業を楽にしたいですか？</h1>
                <ProjectCreationProfile v-model="creationProfile" />
                <ReferenceImageInput v-if="creationProfile.app_type !== 'records'"
                  v-model="referenceImages" />
                <v-divider />
                <v-text-field v-model="name" label="アプリの名前" placeholder="例：備品貸出管理" maxlength="80" counter />
                <v-btn color="primary" :disabled="!validProfile || !name.trim()" @click="step = 2">次へ</v-btn>
              </div>
              <div v-if="step === 2" class="form-stack">
                <h1>{{ creationProfile.app_pattern === 'data_management'
                  ? '帳票やExcelから入力項目を確認します' : '使うデータを確認します' }}</h1>
                <SampleDataImport v-if="creationProfile.app_pattern !== 'data_management'"
                  :profile="creationProfile" @update:profile="value => creationProfile = value"
                  @apply-fields="applySampleFields" />
                <!-- 手元のデータを処理するアプリは、CSV・Excelが無いこともある（PDFを読み取るなど）。
                     そのときは保存する項目を直接決めるか、PDFの見本から読み取る。 -->
                <template v-if="creationProfile.app_pattern !== 'local_file_visualization'">
                  <p v-if="creationProfile.app_pattern === 'file_import'">
                    CSV・Excelが無い場合（PDFや文章を読み取るアプリなど）は、保存する項目を直接設定するか、
                    PDFの見本から読み取ってください。CSV・Excelを読み込んだ場合は、その列が下の項目に入ります。</p>
                  <p v-else>既存の帳票・台帳から読み取るか、入力項目を直接設定してください。</p>
                  <div class="table-actions">
                    <v-btn variant="outlined" prepend-icon="mdi-table-plus" :disabled="tables.length >= 12"
                      @click="tables.push({ name: `帳票${tables.filter(t => t.kind === 'record').length + 1}`, kind: 'record', fields: blankFields() }); tableTab = tables.length - 1">帳票を追加</v-btn>
                    <v-btn variant="outlined" prepend-icon="mdi-database-plus" :disabled="tables.length >= 12"
                      @click="tables.push({ name: `マスター${tables.filter(t => t.kind === 'master').length + 1}`, kind: 'master', fields: blankFields() }); tableTab = tables.length - 1">マスターを追加</v-btn>
                  </div>
                  <v-tabs v-model="tableTab" show-arrows density="comfortable">
                    <v-tab v-for="(item, index) in tables" :key="index" :value="index">
                      <v-icon :icon="item.kind === 'master' ? 'mdi-database-outline' : 'mdi-file-document-outline'" start />{{ item.name || '名称未入力' }}
                    </v-tab>
                  </v-tabs>
                  <div v-if="tables[tableTab]" class="table-editor">
                    <div class="table-heading">
                      <v-text-field v-model="tables[tableTab].name" label="帳票・マスター名" maxlength="60" />
                      <v-select v-model="tables[tableTab].kind" :items="[{ title: '帳票', value: 'record' }, { title: 'マスター', value: 'master' }]" label="種類" />
                      <v-btn variant="text" color="error" :disabled="tables.length === 1"
                        @click="tables.splice(tableTab, 1); tableTab = Math.max(0, tableTab - 1)">この表を外す</v-btn>
                    </div>
                    <SpreadsheetFieldImport @apply="value => { tables[tableTab].fields = value }" />
                    <PdfFieldImport :enabled="!!config?.pdf_extraction_enabled" @apply="value => { tables[tableTab].fields = value }" />
                    <div v-for="(field, index) in fields" :key="index" class="field-row">
                      <v-text-field v-model="field.name" :label="`項目${index + 1}の名前`" maxlength="50" />
                      <v-select v-model="field.kind" :items="kindOptions" label="入力する内容" />
                      <v-checkbox v-model="field.required" label="必須" />
                      <v-btn icon="mdi-minus-circle-outline" variant="text" :disabled="fields.length === 1" :aria-label="`項目${index + 1}を外す`" @click="fields.splice(index, 1)" />
                    </div>
                    <v-btn variant="outlined" prepend-icon="mdi-plus" :disabled="fields.length >= 20"
                      @click="fields.push({ name: '', kind: 'text', required: true })">項目を追加</v-btn>
                  </div>
                </template>
                <p class="tip">{{ creationProfile.app_pattern === 'data_management'
                  ? '作成日時・更新日時は自動で記録します。'
                  : creationProfile.app_pattern === 'file_import'
                    ? '見本のファイルとデータは保存せず、確認した項目の構成だけを仕様に使います。'
                    : '元ファイルとデータは保存せず、確認した列の構成だけを仕様に使います。' }}
                  ここではまだアプリの生成を開始しません。</p>
                <div><v-btn variant="text" @click="step = 1">戻る</v-btn><v-btn color="primary"
                  :loading="purposeDrafting" :disabled="!validFields" @click="preparePurpose">
                  目的の下書きを作る</v-btn></div>
              </div>
              <div v-if="step === 3" class="form-stack interview-step">
                <div>
                  <h1>何のために使うかを確認します</h1>
                  <p class="mt-2">選択内容とデータ項目からAIが作った下書きです。実際の仕事に合う表現へ修正してください。</p>
                </div>
                <v-alert v-if="purposeDraftError" type="warning" variant="tonal">
                  {{ purposeDraftError }} 下書きを修正してそのまま進めます。</v-alert>
                <v-textarea v-model="purpose" label="何のために使いますか？"
                  placeholder="このアプリを使う人、行う仕事、改善したいことを入力してください"
                  maxlength="2000" counter rows="3" />
                <div class="step-actions">
                  <v-btn variant="outlined" prepend-icon="mdi-auto-fix" :loading="purposeDrafting"
                    @click="generatePurposeDraft">AIで下書きを作り直す</v-btn>
                  <!-- 属するテナントが1つしか無いなら選ばせない。選びようがない選択肢は出さない。 -->
                  <v-select v-if="!editing && tenantChoices.length > 1" v-model="tenantId"
                    :items="tenantChoices" label="どのテナントのアプリですか？"
                    hint="利用量をこの単位で数えます。あとから移せます。" persistent-hint />
                  <v-btn v-if="!purposeSaved" color="primary" :loading="loading"
                    :disabled="purpose.trim().length < 5"
                    @click="persistForInterview">{{ selected ? '目的を保存' : '保存してヒアリングへ' }}</v-btn>
                </div>
                <template v-if="selected && purposeSaved">
                  <v-divider />
                  <div>
                    <h2>要件を確認しますか？</h2>
                    <p class="mt-2">AIが帳票・マスターを確認し、実装に必要な判断だけを選択式で質問します。
                      ここでは仕様を確定しません。確定は仕様画面で行います。</p>
                  </div>
                  <RequirementsInterview v-if="user.can_use_codex || config?.gemini_available"
                    :project="selected" :can-use-codex="user.can_use_codex"
                    :gemini-available="!!config?.gemini_available" @applied="interviewApplied" />
                  <v-alert v-else type="info" variant="tonal">利用できるAIが設定されていません。ヒアリングを省略して、仕様画面で確定できます。</v-alert>
                </template>
                <v-divider />
                <!-- 確定は仕様画面にある。同じ操作を2か所に置くと、どちらが本当の
                     確定なのか分からなくなる（省略して確定する導線がここにあった）。 -->
                <div class="step-actions">
                  <v-btn variant="text" @click="step = 2">帳票画面へ戻る</v-btn>
                  <v-btn v-if="selected && purposeSaved" color="primary" append-icon="mdi-arrow-right"
                    @click="openSpecification">仕様画面へ進む</v-btn>
                </div>
              </div>
            </v-card>
          </template>
          <template v-else-if="selected">
            <div class="page-heading mb-4">
              <v-btn variant="text" prepend-icon="mdi-arrow-left" @click="selected = undefined">一覧に戻る</v-btn>
              <h1>{{ selected.name }}</h1>
            </div>
            <!-- 触れる人は開発画面。オーナーだけで判定すると、共同開発者まで
                 閲覧専用になってしまう（実際そうなっていた）。可否はAPIが返す。 -->
            <ProjectReadOnly v-if="!selected.can_edit" :key="selected.id" :project="selected" />
            <DevelopWorkspace v-else :key="selected.id" :project="selected"
              :enabled="!!config?.generation_ready" :preview-enabled="!!config?.preview_enabled"
              :shell-enabled="!!config?.preview_shell_enabled" :local-codex-enabled="!!config?.local_codex_enabled"
              @connect="tab = 'connections'" @edit-spec="editSelected"
              @save-requirements="updateRequirements" @save-prompt="updateGenerationPrompt">
              <template #approval>
                <div v-if="selected.status === 'draft'" class="approval-bar">
                  <div class="approval-copy">
                    <strong><v-icon icon="mdi-alert-circle-outline" size="small" />仕様を確認して確定</strong>
                    <span>確定すると生成できます。{{ selected.creation_profile?.mode === 'prompt'
                      ? '内容を変えるときは依頼文を編集してください。' : '先に要件を詰める場合はヒアリングへ。' }}</span>
                  </div>
                  <div class="approval-actions">
                    <v-btn v-if="selected.creation_profile?.mode !== 'prompt'" color="primary" variant="tonal"
                      prepend-icon="mdi-comment-question-outline" @click="resumeInterview">ヒアリング画面を開く</v-btn>
                    <v-checkbox v-model="consent" label="この仕様で進める" color="warning" base-color="warning"
                      density="compact" hide-details class="consent" />
                    <v-btn class="confirm-button" color="warning" variant="flat" prepend-icon="mdi-check-decagram-outline"
                      :disabled="!consent" :loading="loading" @click="confirm">仕様を確定</v-btn>
                  </div>
                </div>
                <div v-else class="approval-approved">
                  <v-icon icon="mdi-check-circle-outline" color="primary" />
                  <span>第{{ selected.approved_revision }}版を承認済みです。</span>
                </div>
              </template>
              <template #sharing>
                <ProjectSharing :key="selected.id" :project="selected" @updated="applyProjectChange" />
              </template>
            </DevelopWorkspace>
          </template>
        </template>
        <template v-if="tab === 'connections'">
          <h1 class="mb-5">接続・公開環境</h1>
          <CodexConnection v-if="user.can_use_codex" />
          <p v-else class="tip mb-5">このアカウントではCodexを利用できません。アプリ生成ではGeminiを選択できます。</p>
          <ClusterStatus v-if="user.can_manage_users" />
        </template>
        <template v-if="tab === 'network-audit' && user.can_manage_users">
          <h1 class="mb-5">通信ログ</h1>
          <p class="tip mb-5">生成AI Podに関係する通信だけを、接続先と通信結果ごとに確認できます。</p>
          <NetworkAudit />
        </template>
        <template v-if="tab === 'masters' && user.can_manage_users">
          <MasterAdmin :current-user-id="user.id" />
        </template>
        <template v-if="tab === 'tenant-admin' && user.admin_tenant_ids?.length">
          <TenantAdmin :admin-tenant-ids="user.admin_tenant_ids" />
        </template>
      </template>
      <!-- タブの外に置く。中に入れると、そのタブを開いているときしか開けない。 -->
      <v-dialog :model-value="!!deleting" max-width="560"
        @update:model-value="value => { if (!value) deleting = undefined }">
        <v-card class="pa-6">
          <h2>「{{ deleting?.name }}」を削除しますか？</h2>
          <p class="my-4">仕様、生成履歴、生成したコード、プレビューの実行環境をすべて削除します。元に戻せません。</p>
          <v-text-field v-model="confirmName" :label="`確認のためアプリ名を入力：${deleting?.name}`" />
          <v-card-actions>
            <v-btn @click="deleting = undefined">キャンセル</v-btn>
            <v-btn color="error" :loading="loading" :disabled="confirmName.trim() !== deleting?.name"
              @click="confirmDelete">削除する</v-btn>
          </v-card-actions>
        </v-card>
      </v-dialog>
    </v-container>
  </v-main>
</template>

<style scoped>
.forge-container { max-width: var(--forge-content-width); padding-top: var(--sp-8); padding-bottom: var(--sp-10); }
/* 開発画面は左右に並べるため、ウィンドウ幅をそのまま使う。 */
.forge-container--wide { max-width: none; }
.forge-container--workspace { padding-bottom: var(--sp-4); }
.forge-container--broad { max-width: var(--forge-table-width); }
/* アプリケーションバーへ入れたタブ。銘板と操作の間で伸び縮みさせる。 */
.brand { flex: 0 0 auto; margin-inline-end: var(--sp-6); }
/* 銘は記号と文字で一組。間が開くと、別々の部品に見える。 */
.brand :deep(.v-toolbar-title__placeholder) { display: flex; align-items: center; gap: var(--sp-2); }
.app-name { color: var(--ink-1); font-family: Georgia, 'Times New Roman', serif; font-size: var(--fs-lg); font-weight: var(--fw-bold); letter-spacing: .01em; }
.bar-tabs { flex: 0 1 auto; }
.runtime-status { display: flex; gap: var(--sp-2); margin-right: var(--sp-3); white-space: nowrap; }
.runtime-status strong { margin-left: var(--sp-1); font-size: var(--fs-2xs); }
.runtime-chip--starting .v-icon { animation: runtime-pulse 1.4s ease-in-out infinite; }
@keyframes runtime-pulse { 50% { opacity: .35; } }
@media (prefers-reduced-motion: reduce) { .runtime-chip--starting .v-icon { animation: none; } }
/* バーの中では幅を決めておく。任せると伸びきってタブを押し出す。
   狭い画面ではアイコンだけ残し、選べること自体は保つ。 */
.bar-filter { flex: 0 0 auto; width: 15rem; margin-right: var(--sp-2); }
@media (max-width: 900px) {
  .bar-filter { width: 3.5rem; }
  .runtime-status .v-chip { padding-inline: var(--sp-1); }
  .runtime-status strong { display: none; }
}
.eyebrow { font-size: var(--fs-xs); color: var(--text-brand); font-weight: var(--fw-bold); margin-bottom: var(--sp-3); }
h1 { font-size: var(--fs-2xl); } h2 { font-size: var(--fs-lg); }
.subheading { color: var(--ink-3); margin-top: var(--sp-3); }
.page-heading { display: flex; justify-content: space-between; align-items: center; gap: var(--sp-5); flex-wrap: wrap; }
.heading-actions, .empty-actions { display: flex; gap: var(--sp-3); flex-wrap: wrap; }
.empty-actions { justify-content: center; }
.card-actions { display: flex; gap: var(--sp-3); align-items: center; flex-wrap: wrap; }
.alert-content { display: flex; justify-content: space-between; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }
/* 確定はここでしかできない。未確定であることが目に入るよう、注意の色で囲む。
   赤（danger）は削除・エラーの色なので使わない。確定が危ない操作に見えてしまう。 */
.approval-bar { display: flex; align-items: center; justify-content: space-between;
  gap: var(--sp-4); min-width: 0; padding: var(--sp-3) var(--sp-4);
  background: var(--surface-warn); border: 1px solid var(--border-warn);
  border-left: 4px solid var(--surface-warn-solid); border-radius: var(--radius-md); }
.approval-copy strong { display: flex; align-items: center; gap: var(--sp-1); color: var(--text-warn); }
.consent :deep(.v-label) { color: var(--text-warn); font-weight: var(--fw-bold); opacity: 1; }
.approval-copy { display: flex; flex: 1 1 16rem; min-width: 12rem; flex-direction: column;
  gap: var(--sp-1); }
.approval-copy span { color: var(--ink-3); font-size: var(--fs-xs); }
.approval-actions { display: flex; flex: 0 1 auto; align-items: center; justify-content: flex-end;
  gap: var(--sp-3); flex-wrap: wrap; }
.approval-actions :deep(.v-selection-control) { min-height: var(--control-sm); }
.confirm-button.v-btn--disabled { opacity: .68; }
.approval-approved { display: flex; align-items: center; gap: var(--sp-2); color: var(--text-brand);
  font-weight: var(--fw-medium); }
.summary-grid, .project-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(var(--forge-card-min), 1fr)); gap: var(--sp-4); }
.summary-number { font-size: var(--fs-2xl); display: block; margin-top: var(--sp-3); }
.meta { color: var(--ink-3); font-size: var(--fs-xs); }
.form-stack { display: flex; flex-direction: column; gap: var(--sp-5); }
.interview-step { max-width: 900px; }
.step-actions { display: flex; justify-content: space-between; gap: var(--sp-3); flex-wrap: wrap; }
.field-row { display: grid; grid-template-columns: 2fr 2fr 1fr auto; gap: var(--sp-3); align-items: center; }
.table-actions, .table-heading { display: flex; gap: var(--sp-3); align-items: center; flex-wrap: wrap; }
.table-heading > :first-child { flex: 2 1 280px; }
.table-heading > :nth-child(2) { flex: 1 1 180px; }
.table-editor { border: 1px solid var(--border-default); border-radius: var(--radius-lg); padding: var(--sp-5); display: flex; flex-direction: column; gap: var(--sp-4); }
.choice { padding: var(--sp-4); cursor: pointer; } .choice p { margin-left: var(--sp-10); color: var(--ink-3); }
.chosen { outline: 2px solid var(--border-brand); background: var(--surface-accent); }
@media (max-width: 900px) {
  .approval-bar { align-items: stretch; flex-direction: column; }
  .approval-actions { justify-content: flex-start; }
}
@media (max-width: 600px) { .field-row { grid-template-columns: 1fr 1fr; } }
</style>
