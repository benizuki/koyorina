<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import TenantSettings from '@/components/TenantSettings.vue'
import { api } from '@/composables/useApi'
import type { AiUsageSummary, Tenant } from '@/types'

// テナント管理者の画面。自分が管理者のテナントだけを並べ、AI 設定を開けるようにする。
// システム設定の利用者・部門などはシステム管理者だけのまま。
const props = defineProps<{ adminTenantIds: readonly string[] }>()
const tenants = ref<Tenant[]>([]), loading = ref(false), error = ref('')
const opened = ref<Tenant>()
type TenantMember = { id: string; email: string; name: string; roles: string[]; editable: boolean }
const rolesTenant = ref<Tenant>(), members = ref<TenantMember[]>([])
const rolesBusy = ref(false), rolesError = ref('')
const roleChoices = [
  { title: '管理者', value: 'admin' }, { title: '開発者', value: 'developer' },
  { title: '運用管理者', value: 'operator' }, { title: '利用者', value: 'user' },
]
async function openRoles(tenant: Tenant) {
  rolesTenant.value = tenant; rolesError.value = ''; rolesBusy.value = true
  try { members.value = await api<TenantMember[]>(`/api/tenants/${tenant.id}/members`) }
  catch (e) { rolesError.value = e instanceof Error ? e.message : 'ロールを取得できません。' }
  finally { rolesBusy.value = false }
}
async function saveRoles(member: TenantMember) {
  if (!rolesTenant.value || !member.roles.length) return
  rolesBusy.value = true; rolesError.value = ''
  try { await api(`/api/tenants/${rolesTenant.value.id}/members/${member.id}/roles`, 'PUT', { roles: member.roles }) }
  catch (e) { rolesError.value = e instanceof Error ? e.message : 'ロールを保存できません。' }
  finally { rolesBusy.value = false }
}
// 管理しているテナントの分だけの利用状況（直近30日）。サーバーが権限で絞って返す。
const usage = ref<AiUsageSummary>()
const number = (value: number) => value.toLocaleString('ja-JP')
const providerLabel = (provider: string) => ({ codex: 'Codex', gemini: 'Gemini', antigravity: 'Antigravity',
  openai_compatible: 'OpenAI互換API', claude: 'Claude' }[provider] ?? provider)
const mine = computed(() => tenants.value.filter(t => props.adminTenantIds.includes(t.id)))
onMounted(async () => {
  loading.value = true
  try {
    tenants.value = await api<Tenant[]>('/api/tenants')
    usage.value = await api<AiUsageSummary>('/api/ai-usage?days=30')
  }
  catch (e) { error.value = e instanceof Error ? e.message : 'テナントを取得できません。' }
  finally { loading.value = false }
})
</script>

<template>
  <h1 class="mb-3">テナント設定</h1>
  <p class="tip mb-5">
    <v-icon icon="mdi-information-outline" />
    <span>テナント管理者として、利用者のロールと、アプリの生成に使う AI の接続先・キー、生成したアプリが使う Gemini を設定できます。
      設定しなければシステムの既定を使います。</span>
  </p>
  <v-alert v-if="error" type="error" density="compact" class="mb-3">{{ error }}</v-alert>
  <v-card>
    <v-progress-linear v-if="loading" indeterminate color="primary" />
    <v-table>
      <thead><tr><th>テナント</th><th>備考</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="tenant in mine" :key="tenant.id">
          <td>{{ tenant.name }}</td>
          <td>{{ tenant.note || '—' }}</td>
          <td><v-btn variant="text" size="small" prepend-icon="mdi-creation-outline"
            @click="opened = tenant">AI設定</v-btn>
            <v-btn variant="text" size="small" prepend-icon="mdi-account-key-outline"
              @click="openRoles(tenant)">ロール管理</v-btn></td>
        </tr>
      </tbody>
    </v-table>
  </v-card>
  <h2 class="mt-8 mb-3">AI の利用状況（直近30日）</h2>
  <v-card v-if="usage">
    <v-table>
      <thead><tr><th>テナント</th><th class="numeric">依頼</th><th class="numeric">失敗</th>
        <th class="numeric">入力トークン</th><th class="numeric">出力トークン</th></tr></thead>
      <tbody>
        <tr v-for="row in usage.tenants" :key="row.tenant_id ?? 'none'">
          <td>{{ row.tenant_name }}</td><td class="numeric">{{ number(row.requests) }}</td>
          <td class="numeric">{{ number(row.failed) }}</td><td class="numeric">{{ number(row.input_tokens) }}</td>
          <td class="numeric">{{ number(row.output_tokens) }}</td>
        </tr>
        <tr v-if="!usage.tenants.length"><td colspan="5" class="meta">まだ生成の記録がありません。</td></tr>
      </tbody>
    </v-table>
    <v-table>
      <thead><tr><th>生成AI</th><th class="numeric">依頼</th><th class="numeric">合計トークン</th></tr></thead>
      <tbody>
        <tr v-for="row in usage.providers.filter(p => p.requests)" :key="row.provider">
          <td>{{ providerLabel(row.provider) }}</td><td class="numeric">{{ number(row.requests) }}</td>
          <td class="numeric">{{ number(row.total_tokens) }}</td>
        </tr>
      </tbody>
    </v-table>
  </v-card>
  <v-dialog :model-value="!!opened" max-width="1000" scrollable
    @update:model-value="value => { if (!value) opened = undefined }">
    <TenantSettings v-if="opened" :key="opened.id" :tenant="opened" @close="opened = undefined" />
  </v-dialog>
  <v-dialog :model-value="!!rolesTenant" max-width="900" scrollable
    @update:model-value="value => { if (!value) rolesTenant = undefined }">
    <v-card :title="`${rolesTenant?.name ?? ''}のロール管理`" class="pa-4">
      <p class="tip mb-3">既に所属する利用者の役割を個別に設定します。所属の追加・削除はシステム管理者が行います。</p>
      <v-alert v-if="rolesError" type="error" class="mb-3">{{ rolesError }}</v-alert>
      <v-progress-linear v-if="rolesBusy" indeterminate />
      <v-table><thead><tr><th>利用者</th><th>ロール</th><th>操作</th></tr></thead>
        <tbody><tr v-for="member in members" :key="member.id">
          <td>{{ member.name }}<div class="meta">{{ member.email }}</div></td>
          <td><v-select v-model="member.roles" :items="roleChoices" multiple chips closable-chips
            :aria-label="`${member.name}のロール`" :disabled="rolesBusy || !member.editable" hide-details /></td>
          <td><v-btn variant="outlined" size="small" :disabled="rolesBusy || !member.editable || !member.roles.length"
            @click="saveRoles(member)">保存</v-btn></td>
        </tr><tr v-if="!members.length"><td colspan="3">所属する利用者はいません。</td></tr></tbody>
      </v-table>
      <v-card-actions><v-spacer /><v-btn variant="text" @click="rolesTenant = undefined">閉じる</v-btn></v-card-actions>
    </v-card>
  </v-dialog>
</template>

<style scoped>
.numeric { text-align: right; }
.meta { color: var(--ink-4); font-size: var(--fs-xs); }
</style>
