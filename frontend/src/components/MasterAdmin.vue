<script setup lang="ts">
import SystemRegistry from '@/components/SystemRegistry.vue'
import SystemLlmSettings from '@/components/SystemLlmSettings.vue'
import TenantSettings from '@/components/TenantSettings.vue'
import { computed, onMounted, ref, watch } from 'vue'
import { useMasters } from '@/composables/useMasters'
import { api } from '@/composables/useApi'
import type { Department, ManagedUser, Tenant } from '@/types'
import { APP_NAME } from '@/branding'
const props = defineProps<{ currentUserId: string }>()
const { users, departments, roles, tenantRoles, tenants, usage, storage, loading, error, refresh, refreshUsage, refreshStorage, saveUser,
        saveDepartment, removeDepartment, removeUser, saveTenant, removeTenant } = useMasters()
const tab = ref('users')
const dateText = (value: Date) => `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`
const usageEnd = ref(dateText(new Date()))
const usageStart = ref(dateText(new Date(Date.now() - 29 * 24 * 60 * 60 * 1000)))
const editing = ref<Partial<ManagedUser>>()
const editingDepartment = ref<Partial<Department>>()
const editingTenant = ref<Partial<Tenant>>()
const removingTenant = ref<Tenant>()
// AI 設定（生成に使うAI・生成アプリが使うGemini）を開いているテナント。
const aiTenant = ref<Tenant>()
const removing = ref<Department>()
const removingUser = ref<ManagedUser>()
type SupportSession = { id: string; tenant_id: string; permission: 'inspect' | 'repair'
  reason: string; created_at: string; expires_at: string; revoked_at: string | null }
const supportSessions = ref<SupportSession[]>([])
const supportTenant = ref<string>()
const supportPermission = ref<'inspect' | 'repair'>('inspect')
const supportReason = ref('')
const supportLoading = ref(false)
// 所属に選べるのは使う状態の組織だけ。閉じた組織を新しく割り当てさせない。
const choices = computed(() => [{ title: '未設定', value: null },
  ...departments.value.filter(d => d.enabled || d.id === editing.value?.department_id)
    .map(d => ({ title: d.name, value: d.id }))])
const roleName = (id: string) => roles.value.find(r => r.id === id)?.label ?? id
const systemRoleChoices = computed(() => roles.value.map(r => ({ title: `${r.id}（${r.label}）`, value: r.id })))
// テナントでのロールは短い名前で並べる（説明は選択肢の副題に出す）。
const TENANT_ROLE_NAMES: Record<string, string> = { admin: '管理者', developer: '開発者', operator: '運用管理者', user: '利用者' }
const tenantRoleName = (id: string) => TENANT_ROLE_NAMES[id] ?? id
const tenantRoleChoices = computed(() => tenantRoles.value.map(r => ({
  title: tenantRoleName(r.id), value: r.id, props: { subtitle: r.label } })))
// 行に出すのは使う状態のテナントと、閉じていても既に所属しているテナント。
// 閉じたテナントを新しく割り当てさせない。
const tenantRows = computed(() => tenants.value
  .filter(t => t.enabled || membershipOf(t.id)))
function membershipOf(tenantId: string) {
  return editing.value?.tenants?.find(item => item.tenant_id === tenantId)
}
function toggleMembership(tenantId: string, member: boolean | null) {
  if (!editing.value) return
  const rest = (editing.value.tenants ?? []).filter(item => item.tenant_id !== tenantId)
  // 新しく所属させるときは、いちばん弱い「利用者」から始める。
  editing.value.tenants = member ? [...rest, { tenant_id: tenantId, role: 'user', roles: ['user'] }] : rest
}
function setTenantRoles(tenantId: string, roles: string[]) {
  const membership = membershipOf(tenantId)
  if (membership && roles.length) {
    membership.roles = roles
    membership.role = roles[0]
  }
}
// サポートの対象に選べるのは使う状態のテナント。
const supportTenantChoices = computed(() => tenants.value.filter(t => t.enabled)
  .map(t => ({ title: t.name, value: t.id })))
const shortId = (id: string) => id === '00000000-0000-4000-8000-000000000001' ? 'default' : id.slice(0, 8)
const tenantSummary = (user: ManagedUser) => (user.tenants ?? [])
  .map(item => `${tenants.value.find(t => t.id === item.tenant_id)?.name ?? item.tenant_id}（${(item.roles ?? [item.role]).map(tenantRoleName).join('・')}）`)
  .join('、')
function editUser(user: ManagedUser) {
  // 行の中身を直接書き換えないよう、所属の配列も写してから編集する。
  editing.value = { ...user, tenants: (user.tenants ?? []).map(item => ({ ...item, roles: [...(item.roles ?? [item.role])] })) }
}
function newUser() {
  editing.value = { role: 'member', enabled: true, codex_enabled: true, department_id: null, tenants: [] }
}
function newDepartment() { editingDepartment.value = { enabled: true, note: '' } }
function newTenant() { editingTenant.value = { enabled: true, note: '' } }
async function applyTenant() {
  if (await saveTenant(editingTenant.value ?? {})) editingTenant.value = undefined
}
async function applyTenantRemoval() {
  if (removingTenant.value && await removeTenant(removingTenant.value)) removingTenant.value = undefined
}
async function applyUser() {
  if (await saveUser(editing.value ?? {})) editing.value = undefined
}
async function applyDepartment() {
  if (await saveDepartment(editingDepartment.value ?? {})) editingDepartment.value = undefined
}
async function applyRemoval() {
  if (removing.value && await removeDepartment(removing.value)) removing.value = undefined
}
async function applyUserRemoval() {
  if (removingUser.value && await removeUser(removingUser.value)) removingUser.value = undefined
}
async function loadSupport() {
  supportLoading.value = true
  try { supportSessions.value = await api<SupportSession[]>('/api/admin/support-sessions') }
  catch (e) { error.value = e instanceof Error ? e.message : 'サポートセッションを取得できません。' }
  finally { supportLoading.value = false }
}
async function startSupport() {
  if (!supportTenant.value || supportReason.value.trim().length < 10) return
  supportLoading.value = true
  try {
    await api('/api/admin/support-sessions', 'POST', { tenant_id: supportTenant.value,
      permission: supportPermission.value, reason: supportReason.value.trim() })
    supportReason.value = ''
    await loadSupport()
  } catch (e) { error.value = e instanceof Error ? e.message : 'サポートセッションを開始できません。' }
  finally { supportLoading.value = false }
}
async function endSupport(id: string) {
  supportLoading.value = true
  try { await api(`/api/admin/support-sessions/${id}`, 'DELETE'); await loadSupport() }
  catch (e) { error.value = e instanceof Error ? e.message : 'サポートセッションを終了できません。' }
  finally { supportLoading.value = false }
}
const supportActive = (session: SupportSession) => !session.revoked_at
  && new Date(session.expires_at).getTime() > Date.now()
const tenantName = (id: string) => tenants.value.find(item => item.id === id)?.name ?? id
onMounted(refresh)
const loadUsage = () => refreshUsage(usageStart.value, usageEnd.value)
watch(tab, value => {
  if (value === 'usage') loadUsage()
  if (value === 'support') loadSupport()
  if (value === 'storage') refreshStorage()
})
const number = (value: number) => value.toLocaleString('ja-JP')
const compact = (value: number) => new Intl.NumberFormat('ja-JP', { notation: 'compact', maximumFractionDigits: 1 }).format(value)
const duration = (seconds: number) => seconds >= 60 ? `${(seconds / 60).toFixed(1)}分` : `${seconds.toFixed(1)}秒`
const providerLabel = (provider: string) => ({ codex: 'Codex', gemini: 'Gemini', antigravity: 'Antigravity', openai_compatible: 'OpenAI互換API', claude: 'Claude' }[provider] ?? provider)
const activeDays = computed(() => usage.value?.daily.filter(day => day.requests > 0) ?? [])
const dailyMax = computed(() => Math.max(1, ...activeDays.value.map(day => day.requests)))
const shortDate = (value: string) => value.slice(5).replace('-', '/')
const bytes = (value: number) => {
  if (!value) return '0 B'
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1)
  return `${(value / 1024 ** index).toFixed(index > 2 ? 1 : 0)} ${units[index]}`
}
const storageStatus = (value: string) => ({ ok: '正常', warning: '注意', danger: '逼迫',
  unknown: '未計測' }[value] ?? value)
const claimText = (claim: { status: string; used_bytes: number; requested_bytes: number }) =>
  claim.status === 'missing' ? '未作成' : claim.status === 'not_measured' ? '未計測' :
  claim.status === 'error' ? '計測失敗' : `${bytes(claim.used_bytes)} / ${bytes(claim.requested_bytes)}`
</script>

<template>
  <h1 class="mb-5">システム設定</h1>
  <v-alert v-if="error" type="error" class="mb-4">{{ error }}</v-alert>
  <v-tabs v-model="tab" density="comfortable" show-arrows class="mb-5">
    <v-tab value="users">ユーザー</v-tab>
    <v-tab value="departments">部門</v-tab>
    <v-tab value="tenants">テナント</v-tab>
    <v-tab value="storage">ストレージ</v-tab>
    <v-tab value="usage">AI利用状況</v-tab>
    <v-tab value="support">サポート</v-tab>
    <v-tab value="registry">レジストリ設定</v-tab>
    <v-tab value="ai-settings">生成AI設定</v-tab>
  </v-tabs>

  <template v-if="tab === 'users'">
    <div class="page-heading mb-4">
      <p>Googleログインと、利用できる役割を登録します。招待メールは送信しません。</p>
      <v-btn color="primary" prepend-icon="mdi-account-plus-outline" @click="newUser">ユーザーを追加</v-btn>
    </div>
    <v-card>
      <v-table>
        <thead><tr><th>メールアドレス</th><th>表示名</th><th>所属組織</th><th>システムロール</th>
          <th>テナントとロール</th><th>Codex</th><th>状態</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="user in users" :key="user.id">
            <td>{{ user.email }}</td>
            <td>{{ user.display_name || '—' }}</td>
            <td>{{ user.department_name || '—' }}</td>
            <td>{{ roleName(user.role) }}</td>
            <td>{{ user.role === 'admin' ? '全テナント（管理者）' : tenantSummary(user) || '—' }}</td>
            <td><span class="chip chip--sm" :class="user.codex_enabled ? 'chip--brand' : 'chip--warn'">
              {{ user.codex_enabled ? '利用可' : '停止' }}</span></td>
            <td><span class="chip chip--sm" :class="user.enabled ? 'chip--brand' : 'chip--warn'">
              {{ user.enabled ? '利用可能' : '停止中' }}</span></td>
            <td><v-btn variant="text" size="small" prepend-icon="mdi-pencil-outline"
              :disabled="loading" @click="editUser(user)">編集</v-btn>
              <v-btn v-if="user.id !== props.currentUserId" variant="text" size="small" color="error"
                prepend-icon="mdi-delete-outline" :disabled="loading"
                @click="error = ''; removingUser = user">削除</v-btn></td>
          </tr>
        </tbody>
      </v-table>
    </v-card>
  </template>

  <template v-if="tab === 'storage'">
    <div class="page-heading mb-4">
      <div><h2>ストレージ利用状況</h2>
        <p>テナント別の生成・プレビュー領域と、保存先ノード全体の空きを確認します。</p></div>
      <v-btn color="primary" prepend-icon="mdi-database-refresh-outline" :loading="loading"
        @click="refreshStorage(true)">再計測</v-btn>
    </div>
    <p class="tip usage-note mb-4"><v-icon icon="mdi-information-outline" />
      <span>local-pathの20Giは要求サイズで、テナントごとの強制上限ではありません。
        実際の停止リスクはノード全体の空き容量でも判断してください。</span></p>
    <div v-if="storage" class="usage-cards storage-cards mb-4">
      <v-card class="metric pa-4"><v-icon icon="mdi-harddisk" /><div>
        <strong>{{ bytes(storage.node.available_bytes) }}</strong><span>ノード空き</span></div></v-card>
      <v-card class="metric pa-4"><v-icon icon="mdi-chart-donut" /><div>
        <strong>{{ storage.node.usage_percent }}%</strong><span>ノード使用率</span></div></v-card>
      <v-card class="metric pa-4"><v-icon icon="mdi-folder-multiple-outline" /><div>
        <strong>{{ bytes(storage.totals.used_bytes) }}</strong><span>テナントデータ合計</span></div></v-card>
      <v-card class="metric pa-4"><v-icon icon="mdi-clock-outline" /><div>
        <strong class="measured-at">{{ storage.measured_at ? new Date(storage.measured_at).toLocaleString('ja-JP') : '未計測' }}</strong>
        <span>最終計測</span></div></v-card>
    </div>
    <v-progress-linear v-if="storage && storage.node.capacity_bytes" class="mb-4" height="10"
      :model-value="storage.node.usage_percent"
      :color="storage.node.level === 'danger' ? 'error' : storage.node.level === 'warning' ? 'warning' : 'primary'" />
    <v-card v-if="storage">
      <div class="card-heading"><strong>テナント別</strong><span>{{ storage.tenants.length }}件</span></div>
      <v-table density="compact" class="storage-table"><thead><tr><th>テナント</th><th>生成領域</th>
        <th>プレビュー領域</th><th>合計</th><th>状態</th><th>計測日時</th></tr></thead>
        <tbody><tr v-for="row in storage.tenants" :key="row.tenant_id">
          <td>{{ row.tenant_name }}</td><td>{{ claimText(row.generation) }}</td>
          <td>{{ claimText(row.preview) }}</td><td>{{ bytes(row.used_bytes) }}</td>
          <td><span class="chip chip--sm" :class="row.level === 'danger' ? 'chip--warn' : 'chip--brand'">
            {{ storageStatus(row.level) }}</span></td>
          <td>{{ row.generation.measured_at ? new Date(row.generation.measured_at).toLocaleString('ja-JP') : '—' }}</td>
        </tr></tbody></v-table>
      <p v-if="!storage.tenants.length" class="empty-usage">テナントがありません。</p>
    </v-card>
  </template>

  <template v-if="tab === 'usage'">
    <div class="usage-heading mb-5">
      <div><p class="eyebrow">AI USAGE</p><h2 class="page-title">AI利用状況</h2>
        <p class="page-lead">アプリ作成で利用したAIの回数、トークン、処理時間を集計します。</p></div>
      <div class="date-controls">
        <v-text-field v-model="usageStart" label="開始日" type="date" density="compact" hide-details />
        <v-text-field v-model="usageEnd" label="終了日" type="date" density="compact" hide-details />
        <v-btn color="primary" prepend-icon="mdi-refresh" :loading="loading" @click="loadUsage">再集計</v-btn>
      </div>
    </div>
    <div v-if="usage" class="usage-cards mb-4">
      <v-card class="metric pa-4"><v-icon icon="mdi-creation-outline" /><div><strong>{{ number(usage.totals.requests) }}</strong><span>期間内の呼び出し</span></div></v-card>
      <v-card class="metric pa-4"><v-icon icon="mdi-text-box-outline" /><div><strong>{{ compact(usage.totals.total_tokens) }}</strong><span>合計トークン</span></div></v-card>
      <v-card class="metric pa-4"><v-icon icon="mdi-timer-outline" /><div><strong>{{ duration(usage.totals.duration_seconds) }}</strong><span>合計処理時間</span></div></v-card>
      <v-card class="metric pa-4"><v-icon icon="mdi-calendar-today-outline" /><div><strong>{{ number(usage.today_requests) }}</strong><span>本日の利用</span></div></v-card>
    </div>
    <p v-if="usage" class="tip usage-note mb-4"><v-icon icon="mdi-information-outline" />
      <span>{{ APP_NAME }}全体の利用状況です。トークン数は実行環境から取得できた生成を集計しています。</span></p>
    <v-card v-if="usage" class="chart-card mb-4">
      <div class="card-heading"><strong>日別の呼び出し</strong><span>{{ usage.start }} ～ {{ usage.end }}</span></div>
      <div v-if="activeDays.length" class="usage-chart">
        <div v-for="day in activeDays" :key="day.date" class="bar-column">
          <span class="bar-value">{{ day.requests }}</span>
          <span class="bar" :style="{ height: `${Math.max(8, day.requests / dailyMax * 100)}%` }" />
          <span class="bar-label">{{ shortDate(day.date) }}</span>
        </div>
      </div>
      <p v-else class="empty-usage">この期間のAI利用はありません。</p>
    </v-card>
    <div v-if="usage" class="usage-tables">
      <v-card>
        <div class="card-heading"><strong>AI別</strong><span>3種類</span></div>
        <v-table density="comfortable"><thead><tr><th>AI</th><th>回数</th><th>成功</th><th>失敗</th>
          <th>入力</th><th>出力</th><th>合計トークン</th><th>処理時間</th></tr></thead>
          <tbody><tr v-for="row in usage.providers" :key="row.provider">
            <td>{{ providerLabel(row.provider) }}</td><td>{{ number(row.requests) }}</td>
            <td>{{ number(row.succeeded) }}</td><td :class="{ 'error-count': row.failed }">{{ number(row.failed) }}</td>
            <td>{{ row.tokenized_requests ? compact(row.input_tokens) : '未取得' }}</td>
            <td>{{ row.tokenized_requests ? compact(row.output_tokens) : '未取得' }}</td>
            <td>{{ row.tokenized_requests ? compact(row.total_tokens) : '未取得' }}</td>
            <td>{{ duration(row.duration_seconds) }}</td>
          </tr></tbody></v-table>
      </v-card>
      <v-card>
        <div class="card-heading"><strong>テナント別</strong><span>{{ usage.tenants.length }}件</span></div>
        <v-table density="comfortable"><thead><tr><th>テナント</th><th>アプリ</th><th>回数</th><th>トークン</th><th>処理時間</th></tr></thead>
          <tbody><tr v-for="row in usage.tenants" :key="row.tenant_id ?? 'none'"><td>{{ row.tenant_name }}</td>
            <td>{{ number(row.projects) }}</td><td>{{ number(row.requests) }}</td>
            <td>{{ row.tokenized_requests ? compact(row.total_tokens) : '未取得' }}</td>
            <td>{{ duration(row.duration_seconds) }}</td></tr></tbody></v-table>
        <p v-if="!usage.tenants.length" class="empty-usage">この期間の利用はありません。</p>
      </v-card>
      <v-card>
        <div class="card-heading"><strong>作成アプリ別</strong><span>{{ usage.projects.length }}件</span></div>
        <v-table density="comfortable"><thead><tr><th>作成アプリ</th><th>回数</th><th>トークン</th><th>処理時間</th></tr></thead>
          <tbody><tr v-for="row in usage.projects" :key="row.project_id"><td>{{ row.project_name }}</td>
            <td>{{ number(row.requests) }}</td><td>{{ row.tokenized_requests ? compact(row.total_tokens) : '未取得' }}</td>
            <td>{{ duration(row.duration_seconds) }}</td></tr></tbody></v-table>
        <p v-if="!usage.projects.length" class="empty-usage">この期間の作成アプリはありません。</p>
      </v-card>
      <v-card>
        <div class="card-heading"><strong>利用者別</strong><span>{{ usage.users.filter(row => row.requests).length }}名</span></div>
        <v-table density="comfortable"><thead><tr><th>利用者</th><th>回数</th><th>入力</th><th>出力</th><th>エラー</th><th>Codex</th></tr></thead>
          <tbody><tr v-for="row in usage.users.filter(item => item.requests)" :key="row.user_id">
            <td>{{ row.display_name || row.email }}<br><span class="usage-email">{{ row.email }}</span></td>
            <td>{{ number(row.requests) }}</td><td>{{ compact(row.input_tokens) }}</td><td>{{ compact(row.output_tokens) }}</td>
            <td :class="{ 'error-count': row.failed }">{{ number(row.failed) }}</td>
            <td>{{ row.codex_enabled ? '利用可' : '停止' }}</td></tr></tbody></v-table>
        <p v-if="!usage.users.some(row => row.requests)" class="empty-usage">この期間の利用者はいません。</p>
      </v-card>
    </div>
  </template>

  <template v-if="tab === 'departments'">
    <div class="page-heading mb-4">
      <p>利用者の所属として選べる組織の一覧です。使わなくなった組織は停止できます。</p>
      <v-btn color="primary" prepend-icon="mdi-office-building-outline" @click="newDepartment">部門を追加</v-btn>
    </div>
    <v-card>
      <v-table>
        <thead><tr><th>名称</th><th>備考</th><th>状態</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="department in departments" :key="department.id">
            <td>{{ department.name }}</td>
            <td>{{ department.note || '—' }}</td>
            <td><span class="chip chip--sm" :class="department.enabled ? 'chip--brand' : 'chip--warn'">
              {{ department.enabled ? '選択できる' : '停止中' }}</span></td>
            <td>
              <v-btn variant="text" size="small" prepend-icon="mdi-pencil-outline" :disabled="loading"
                @click="editingDepartment = { ...department }">編集</v-btn>
              <v-btn variant="text" size="small" color="error" prepend-icon="mdi-delete-outline"
                :disabled="loading" @click="removing = department">削除</v-btn>
            </td>
          </tr>
        </tbody>
      </v-table>
    </v-card>
    <p v-if="!departments.length" class="tip mt-4">まだ部門がありません。「部門を追加」から登録してください。</p>
  </template>

  <template v-if="tab === 'tenants'">
    <div class="page-heading mb-4">
      <p>アプリの生成・プレビュー領域を物理的に分離する事業単位です。
        利用者と共同開発者は所属テナントの範囲でだけ設定できます。</p>
      <v-btn color="primary" prepend-icon="mdi-domain" @click="newTenant">テナントを追加</v-btn>
    </div>
    <v-card>
      <v-table>
        <thead><tr><th>名称</th><th>備考</th><th>状態</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="tenant in tenants" :key="tenant.id">
            <td>{{ tenant.name }}</td>
            <td>{{ tenant.note || '—' }}</td>
            <td><span class="chip chip--sm" :class="tenant.enabled ? 'chip--brand' : 'chip--warn'">
              {{ tenant.enabled ? '選択できる' : '停止中' }}</span></td>
            <td>
              <v-btn variant="text" size="small" prepend-icon="mdi-pencil-outline" :disabled="loading"
                @click="editingTenant = { ...tenant }">編集</v-btn>
              <v-btn variant="text" size="small" prepend-icon="mdi-creation-outline" :disabled="loading"
                @click="aiTenant = tenant">AI設定</v-btn>
              <v-btn variant="text" size="small" color="error" prepend-icon="mdi-delete-outline"
                :disabled="loading" @click="removingTenant = tenant">削除</v-btn>
            </td>
          </tr>
        </tbody>
      </v-table>
    </v-card>
    <p v-if="!tenants.length" class="tip mt-4">まだテナントがありません。「テナントを追加」から登録してください。</p>
  </template>

  <SystemRegistry v-if="tab === 'registry'" />
  <SystemLlmSettings v-if="tab === 'ai-settings'" />

  <template v-if="tab === 'support'">
    <div class="page-heading mb-4">
      <p>利用者から調査依頼を受けた場合だけ、理由と対象テナントを指定して60分間アクセスします。
        閲覧・修正内容は監査ログへ記録されます。</p>
    </div>
    <v-card class="pa-5 mb-4">
      <div class="form">
        <v-select v-model="supportTenant" :items="supportTenantChoices" label="対象テナント" />
        <v-select v-model="supportPermission" :items="[
          { title: '閲覧のみ', value: 'inspect' }, { title: '調査と修正', value: 'repair' }]"
          label="権限" />
        <v-textarea v-model="supportReason" class="wide" label="調査理由" rows="2" maxlength="500"
          hint="監査ログへ記録します（10文字以上）" persistent-hint />
      </div>
      <v-btn color="primary" prepend-icon="mdi-shield-account-outline" :loading="supportLoading"
        :disabled="!supportTenant || supportReason.trim().length < 10" @click="startSupport">
        60分のサポートを開始
      </v-btn>
    </v-card>
    <v-card>
      <v-table density="comfortable">
        <thead><tr><th>テナント</th><th>権限</th><th>理由</th><th>期限</th><th>状態</th><th>操作</th></tr></thead>
        <tbody><tr v-for="session in supportSessions" :key="session.id">
          <td>{{ tenantName(session.tenant_id) }}</td>
          <td>{{ session.permission === 'repair' ? '調査と修正' : '閲覧のみ' }}</td>
          <td>{{ session.reason }}</td><td>{{ new Date(session.expires_at).toLocaleString('ja-JP') }}</td>
          <td>{{ supportActive(session) ? '有効' : '終了' }}</td>
          <td><v-btn v-if="supportActive(session)" variant="text" color="error" size="small"
            :disabled="supportLoading" @click="endSupport(session.id)">終了</v-btn></td>
        </tr></tbody>
      </v-table>
      <p v-if="!supportSessions.length" class="empty-usage">サポートセッションはありません。</p>
    </v-card>
  </template>

  <v-dialog :model-value="!!editing" max-width="720"
    @update:model-value="value => { if (!value) editing = undefined }">
    <v-card v-if="editing" class="pa-6">
      <h2>{{ editing.id ? 'ユーザーを編集' : 'ユーザーを追加' }}</h2>
      <p class="lead">Googleログインと、利用できる役割を登録します。</p>
      <div class="form">
        <v-text-field v-model="editing.email" label="メールアドレス" type="email" />
        <v-text-field v-model="editing.display_name" label="表示名" />
        <v-select v-model="editing.department_id" :items="choices" label="所属組織"
          hint="部門マスターから選びます" persistent-hint />
        <v-select v-model="editing.role" :items="systemRoleChoices" label="システムロール" class="wide"
          hint="admin は全テナントを管理します。member はテナントごとのロールに従います。" persistent-hint />
        <v-text-field v-model="editing.google_subject" label="Google Subject（任意）" class="wide"
          hint="初回ログインで自動的に記録します。別アカウントでの利用を防ぎます。" persistent-hint />
      </div>
      <section class="tenant-roles" aria-labelledby="tenant-roles-title">
        <header class="tenant-roles__head">
          <h3 id="tenant-roles-title">テナント権限</h3>
          <p>所属させるテナントにチェックを入れ、必要なロールを個別に選びます。
            管理者・開発者・運用管理者・利用者の権限は独立しています。</p>
        </header>
        <div v-for="tenant in tenantRows" :key="tenant.id" class="tenant-roles__row"
          :class="{ 'is-off': !membershipOf(tenant.id) }">
          <div class="tenant-roles__check">
            <v-checkbox-btn :model-value="!!membershipOf(tenant.id)" color="primary"
              :aria-label="`${tenant.name}に所属させる`"
              @update:model-value="value => toggleMembership(tenant.id, value)" />
          </div>
          <div class="tenant-roles__name">
            <strong>{{ tenant.name }}</strong>
            <span>{{ shortId(tenant.id) }}{{ tenant.enabled ? '' : '・停止中' }}</span>
          </div>
          <!-- v-select はメニューの要素を隣に出す。グリッドの列を増やさないよう包む。 -->
          <div class="tenant-roles__role">
            <v-select :model-value="membershipOf(tenant.id)?.roles ?? ['user']" :items="tenantRoleChoices"
              :disabled="!membershipOf(tenant.id)" density="compact" hide-details variant="outlined"
              multiple chips closable-chips :aria-label="`${tenant.name}でのロール`"
              @update:model-value="value => setTenantRoles(tenant.id, value)" />
          </div>
        </div>
        <p v-if="!tenantRows.length" class="tenant-roles__foot">テナントがありません。テナントのタブで追加します。</p>
        <p v-else-if="editing.role === 'admin'" class="tenant-roles__foot">
          システムロールが admin の間は、ここでの設定にかかわらず全テナントを管理できます。
          member に戻したときに、ここでの所属とロールが使われます。
        </p>
      </section>
      <v-switch v-model="editing.enabled" color="primary" density="compact" hide-details
        :label="editing.enabled ? '利用できる' : '利用を停止中'" />
      <v-switch v-model="editing.codex_enabled" color="primary" density="compact" hide-details
        :label="editing.codex_enabled ? 'Codexを利用できる' : 'Codexの利用を停止中'" />
      <p v-if="editing.id === props.currentUserId" class="tip tip--warn">
        自分自身の管理権限と利用状態は変更できません。
      </p>
      <v-card-actions class="actions">
        <v-btn @click="editing = undefined">キャンセル</v-btn>
        <v-btn color="primary" :loading="loading" :disabled="!editing.email" @click="applyUser">保存する</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

  <v-dialog :model-value="!!editingTenant" max-width="560"
    @update:model-value="value => { if (!value) editingTenant = undefined }">
    <v-card v-if="editingTenant" class="pa-6">
      <h2>{{ editingTenant.id ? 'テナントを編集' : 'テナントを追加' }}</h2>
      <p class="lead">アプリの区分と、利用量を数える単位になります。</p>
      <div class="form form--single">
        <v-text-field v-model="editingTenant.name" label="名称" />
        <v-text-field v-model="editingTenant.note" label="備考（任意）" />
      </div>
      <v-switch v-model="editingTenant.enabled" color="primary" density="compact" hide-details
        :label="editingTenant.enabled ? 'アプリを置ける' : '選択を停止中'" />
      <v-card-actions class="actions">
        <v-btn @click="editingTenant = undefined">キャンセル</v-btn>
        <v-btn color="primary" :loading="loading" :disabled="!editingTenant.name"
          @click="applyTenant">保存する</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

  <v-dialog :model-value="!!aiTenant" max-width="1000" scrollable
    @update:model-value="value => { if (!value) aiTenant = undefined }">
    <TenantSettings v-if="aiTenant" :key="aiTenant.id" :tenant="aiTenant" @close="aiTenant = undefined" />
  </v-dialog>

  <v-dialog :model-value="!!removingTenant" max-width="560"
    @update:model-value="value => { if (!value) removingTenant = undefined }">
    <v-card v-if="removingTenant" class="pa-6">
      <h2>「{{ removingTenant.name }}」を削除しますか？</h2>
      <p class="my-4">このテナントに属するアプリが1つでもあると削除できません。
        区分が消えると、そのアプリの利用量をどこにも数えられなくなるためです。
        使わなくなっただけなら、削除せず「停止中」にしてください。</p>
      <v-card-actions class="actions">
        <v-btn @click="removingTenant = undefined">キャンセル</v-btn>
        <v-btn color="error" :loading="loading" @click="applyTenantRemoval">削除する</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

  <v-dialog :model-value="!!editingDepartment" max-width="560"
    @update:model-value="value => { if (!value) editingDepartment = undefined }">
    <v-card v-if="editingDepartment" class="pa-6">
      <h2>{{ editingDepartment.id ? '部門を編集' : '部門を追加' }}</h2>
      <p class="lead">利用者の所属として選べるようになります。</p>
      <div class="form form--single">
        <v-text-field v-model="editingDepartment.name" label="名称" />
        <v-text-field v-model="editingDepartment.note" label="備考（任意）" />
      </div>
      <v-switch v-model="editingDepartment.enabled" color="primary" density="compact" hide-details
        :label="editingDepartment.enabled ? '所属に選べる' : '選択を停止中'" />
      <v-card-actions class="actions">
        <v-btn @click="editingDepartment = undefined">キャンセル</v-btn>
        <v-btn color="primary" :loading="loading" :disabled="!editingDepartment.name"
          @click="applyDepartment">保存する</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

  <v-dialog :model-value="!!removingUser" max-width="560" :persistent="loading"
    @update:model-value="value => { if (!value) removingUser = undefined }">
    <v-card class="pa-6">
      <h2>ユーザーを削除しますか？</h2>
      <p class="my-4">{{ removingUser?.display_name }}（{{ removingUser?.email }}）</p>
      <p>削除するとログインできなくなります。この操作は取り消せません。</p>
      <p class="my-4">作成アプリや生成履歴がある場合は削除できません。編集画面から利用を停止してください。</p>
      <v-alert v-if="error" type="error" class="mb-4">{{ error }}</v-alert>
      <v-card-actions>
        <v-btn :disabled="loading" @click="removingUser = undefined">キャンセル</v-btn>
        <v-btn color="error" :loading="loading" :disabled="loading" @click="applyUserRemoval">削除する</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

  <v-dialog :model-value="!!removing" max-width="560"
    @update:model-value="value => { if (!value) removing = undefined }">
    <v-card class="pa-6">
      <h2>「{{ removing?.name }}」を削除しますか？</h2>
      <p class="my-4">所属している利用者がいる場合は削除できません。先に所属を変更してください。</p>
      <v-card-actions>
        <v-btn @click="removing = undefined">キャンセル</v-btn>
        <v-btn color="error" :loading="loading" @click="applyRemoval">削除する</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<style scoped>
/* 入力の段は縦横で間隔を変える。横は隣と分かれば足り、縦は補足の文が入るぶん広く取る。 */
.form { display: grid; grid-template-columns: 1fr 1fr; column-gap: var(--sp-5); row-gap: var(--sp-6);
  margin: var(--sp-5) 0 var(--sp-4); }
.form--single { grid-template-columns: 1fr; row-gap: var(--sp-4); }
.wide { grid-column: 1 / -1; }
.lead { color: var(--ink-3); margin-top: var(--sp-2); }
.tip { margin-top: var(--sp-4); }
/* テナントごとの権限。1テナント1行で、チェック・名前・ロールを横に並べる。 */
/* カードは縦のflex。縮むと枠(overflow: hidden)で行が切れるので、縮ませない。 */
.tenant-roles { flex-shrink: 0; margin: 0 0 var(--sp-4); border: 1px solid var(--border-default);
  border-radius: var(--radius-md); overflow: hidden; }
.tenant-roles__head { padding: var(--sp-3) var(--sp-4); background: var(--surface-subtle);
  border-bottom: 1px solid var(--border-subtle); }
.tenant-roles__head h3 { font-size: var(--fs-md); font-weight: var(--fw-bold); }
.tenant-roles__head p { margin-top: var(--sp-1); font-size: var(--fs-sm); color: var(--ink-3); }
.tenant-roles__row { display: grid; grid-template-columns: auto 1fr minmax(9rem, 12rem); align-items: center;
  gap: var(--sp-3); padding: var(--sp-2) var(--sp-4); border-bottom: 1px solid var(--border-subtle); }
.tenant-roles__name { display: flex; flex-direction: column; min-width: 0; }
.tenant-roles__name strong { overflow-wrap: anywhere; }
.tenant-roles__name span { font-size: var(--fs-xs); color: var(--ink-3); }
.tenant-roles__row.is-off .tenant-roles__name { color: var(--ink-3); }
.tenant-roles__foot { padding: var(--sp-3) var(--sp-4); font-size: var(--fs-sm); color: var(--ink-3); }
.actions { padding: 0; margin-top: var(--sp-6); gap: var(--sp-2); }
/* 補足の無い行と揃うよう、切り替えの上に少しだけ余白を置く。 */
.v-switch { margin-top: var(--sp-1); }
.usage-heading { display: flex; align-items: end; justify-content: space-between; gap: var(--sp-5); }
.eyebrow { color: var(--text-brand); font-size: var(--fs-xs); font-weight: var(--fw-bold); letter-spacing: var(--sp-1); }
.date-controls { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)) auto; align-items: center; gap: var(--sp-3); }
.usage-cards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: var(--sp-3); }
.metric { display: flex; align-items: center; gap: var(--sp-3); }
.metric .v-icon { padding: var(--sp-2); box-sizing: content-box; border-radius: var(--radius-md); color: var(--text-brand); background: var(--surface-accent); }
.metric strong { margin-right: var(--sp-2); color: var(--ink-1); font-size: var(--fs-xl); }
.metric strong.measured-at { font-size: var(--fs-sm); }
.metric span { color: var(--ink-3); font-size: var(--fs-sm); }
.storage-table { font-size: var(--fs-sm); }
.usage-note { background: var(--surface-accent); border-color: transparent; }
.card-heading { display: flex; justify-content: space-between; padding: var(--sp-3) var(--sp-4); border-bottom: 1px solid var(--border-default); }
.card-heading span { color: var(--ink-4); font-size: var(--fs-sm); }
.usage-chart { height: calc(var(--sp-10) * 4); display: flex; align-items: end; justify-content: space-around; gap: var(--sp-3); padding: var(--sp-5) var(--sp-6) var(--sp-3); }
.bar-column { height: 100%; min-width: var(--sp-8); display: flex; flex-direction: column; align-items: center; justify-content: end; gap: var(--sp-1); }
.bar { width: var(--sp-8); max-height: calc(100% - var(--sp-8)); background: var(--surface-accent-solid); border-radius: var(--radius-sm) var(--radius-sm) 0 0; }
.bar-value, .bar-label { color: var(--ink-4); font-size: var(--fs-2xs); }
.usage-tables { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-3); }
.empty-usage { padding: var(--sp-6); text-align: center; color: var(--ink-4); }
.usage-email { color: var(--ink-3); }
.error-count { color: var(--text-danger); font-weight: var(--fw-bold); }
@media (max-width: 720px) {
  .form, .usage-cards, .usage-tables, .date-controls { grid-template-columns: 1fr; }
  .tenant-roles__row { grid-template-columns: auto 1fr; }
  .tenant-roles__role { grid-column: 2; }
  .usage-heading { align-items: stretch; flex-direction: column; }
  .usage-chart { overflow-x: auto; justify-content: flex-start; }
}
</style>
