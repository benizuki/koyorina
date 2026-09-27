<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useTenantAi } from '@/composables/useTenantAi'
import type { Tenant, TenantAiSettings } from '@/types'

// テナントの生成アプリが使うGemini。ここで1回設定すれば、アプリを作る人は何も知らずに使える。
// アプリごとの「環境変数」で同じ名前を指定すれば、そのアプリだけ上書きできる。
// embedded はテナントの AI 設定（TenantSettings）の中に置くとき。枠と見出しを付けない。
const props = defineProps<{ tenant: Tenant; embedded?: boolean }>()
const emit = defineEmits<{ close: [] }>()
const ai = useTenantAi(props.tenant.id)

type Form = Omit<TenantAiSettings, 'api_key_configured' | 'secrets_available'>
const form = ref<Form>({ backend: 'none', gcp_project: '', location: 'global', model: 'gemini-3.5-flash',
  thinking_level: '', wif_project_number: '', wif_pool_id: '', wif_provider_id: '', wif_service_account: '' })
const apiKey = ref(''), showGuide = ref(false)
const modelChoices = ['gemini-3.8-flash', 'gemini-3.5-flash']
const locationChoices = ['global', 'us-central1', 'asia-northeast1', 'europe-west4']
const thinkingChoices = [{ title: '指定しない（モデルの既定）', value: '' }, { title: 'MINIMAL', value: 'MINIMAL' },
  { title: 'LOW', value: 'LOW' }, { title: 'MEDIUM', value: 'MEDIUM' }, { title: 'HIGH', value: 'HIGH' }]

watch(ai.settings, value => {
  if (!value) return
  const { api_key_configured: _, secrets_available: __, ...rest } = value
  form.value = { ...rest, location: rest.location || 'global', model: rest.model || 'gemini-3.5-flash' }
})
onMounted(ai.refresh)

const vertex = computed(() => form.value.backend === 'vertex')
const geminiApi = computed(() => form.value.backend === 'gemini_api')
const keyBlocked = computed(() => geminiApi.value && !ai.settings.value?.secrets_available)
const poolPath = computed(() => `projects/${form.value.wif_project_number || '<プロジェクト番号>'}/locations/global/`
  + `workloadIdentityPools/${form.value.wif_pool_id || '<プールID>'}`)
const principal = computed(() => ai.identity.value
  ? `principal://iam.googleapis.com/${poolPath.value}/subject/${ai.identity.value.subject}` : '')
const commands = computed(() => {
  const info = ai.identity.value
  if (!info) return ''
  const pool = form.value.wif_pool_id || '<プールID>', provider = form.value.wif_provider_id || '<プロバイダーID>'
  const poolNumber = form.value.wif_project_number || '<プールを置くプロジェクトの番号>'
  const target = form.value.wif_service_account
  return [
    '# 1. プールとOIDCプロバイダーを作る（プールを置くプロジェクトで実行）',
    '# プロジェクト番号からプロジェクトIDを引く（workload-identity-pools は ID しか受け付けない）',
    `POOL_PROJECT=$(gcloud projects describe ${poolNumber} --format='value(projectId)')`,
    `gcloud iam workload-identity-pools create ${pool} --location=global --project="$POOL_PROJECT"`,
    `gcloud iam workload-identity-pools providers create-oidc ${provider} --location=global \\`,
    `  --workload-identity-pool=${pool} --project="$POOL_PROJECT" \\`,
    `  --issuer-uri=${info.issuer} --jwk-json-path=jwks.json \\`,
    '  --attribute-mapping=google.subject=assertion.sub',
    '',
    target
      ? '# 2. プレビューの身元に、サービスアカウントへのなりすましを許す'
      : '# 2. プレビューの身元に、Vertex AI の利用を許す（Vertex AI を使うプロジェクトで実行）',
    target
      ? `gcloud iam service-accounts add-iam-policy-binding ${target} --role=roles/iam.workloadIdentityUser \\\n  --member=${principal.value}`
      : `gcloud projects add-iam-policy-binding ${form.value.gcp_project || '<GCPプロジェクトID>'} --role=roles/aiplatform.user \\\n  --member=${principal.value}`,
  ].join('\n')
})

async function openGuide() {
  showGuide.value = !showGuide.value
  if (showGuide.value && !ai.identity.value) await ai.loadIdentity()
}
function downloadJwks() {
  if (!ai.identity.value) return
  const url = URL.createObjectURL(new Blob([JSON.stringify(ai.identity.value.jwks, null, 2)],
    { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url; link.download = 'jwks.json'; link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000)
}
async function persist() {
  const ok = await ai.save(form.value, geminiApi.value ? apiKey.value.trim() : '')
  if (ok) apiKey.value = ''
  return ok
}
async function save() {
  if (await persist()) emit('close')
}
// テストは保存済みの設定で行う。画面の値と食い違わないよう、先に保存してから試す。
async function saveAndTest() {
  if (await persist()) await ai.test()
}
const stepLabels: Record<string, string> = {
  settings: '設定の確認', credentials: '資格情報の読み込み', sts: 'トークンの交換（STS）',
  impersonation: 'サービスアカウントへのなりすまし', generate: 'Gemini への問い合わせ', pod: 'テスト用Podの実行',
}
</script>

<template>
  <v-card :class="{ 'pa-6': !embedded }" :border="!embedded" :flat="embedded">
    <h2 v-if="!embedded">生成アプリが使う Gemini（{{ tenant.name }}）</h2>
    <p class="lead">このテナントで作るアプリは、ここで設定した Gemini を何も設定せずに使えます。
      アプリごとの「環境変数」で同じ名前を指定すると、そのアプリだけ上書きできます。
      接続テストは、生成アプリと同じ身元・同じ通信経路で Gemini に1回だけ問い合わせます。</p>
    <v-alert v-if="ai.error.value" type="error" density="compact" class="my-3">{{ ai.error.value }}</v-alert>
    <v-progress-linear v-if="ai.loading.value" indeterminate color="primary" class="my-3" />
    <template v-else>
      <v-radio-group v-model="form.backend" inline hide-details class="my-2" aria-label="接続方式">
        <v-radio label="使わない" value="none" />
        <v-radio label="Gemini API（APIキー）" value="gemini_api" />
        <v-radio label="Vertex AI（Workload Identity 連携）" value="vertex" />
      </v-radio-group>

      <div v-if="form.backend !== 'none'" class="form">
        <v-combobox v-model="form.model" :items="modelChoices" label="モデル" />
        <v-select v-model="form.thinking_level" :items="thinkingChoices" label="思考レベル" />
      </div>

      <template v-if="geminiApi">
        <p v-if="keyBlocked" class="tip tip--warn my-3">
          <v-icon icon="mdi-alert-outline" />
          <span>APIキーを暗号化する鍵（TENANT_SECRET_KEY）が設定されていないため、保存できません。
            運用担当者に設定を依頼するか、Vertex AI を選んでください。</span></p>
        <v-text-field v-model="apiKey" type="password" autocomplete="off" label="Gemini API のAPIキー"
          :placeholder="ai.settings.value?.api_key_configured ? '設定済み（入れ替えるときだけ入力）' : ''"
          :disabled="keyBlocked" persistent-placeholder class="my-3" />
      </template>

      <template v-if="vertex">
        <div class="form">
          <v-text-field v-model="form.gcp_project" label="Vertex AI を使うGCPプロジェクトID"
            hint="Koyorina と別のプロジェクトでも使えます" persistent-hint />
          <v-combobox v-model="form.location" :items="locationChoices" label="リージョン" />
        </div>
        <h3 class="mt-5">Workload Identity 連携</h3>
        <p class="meta my-2">鍵は保存しません。プレビューが名乗る身元を、GCP側で信頼する設定をします。</p>
        <div class="form">
          <v-text-field v-model="form.wif_project_number" label="プールのプロジェクト番号" />
          <v-text-field v-model="form.wif_pool_id" label="プールID" />
          <v-text-field v-model="form.wif_provider_id" label="プロバイダーID" />
          <v-text-field v-model="form.wif_service_account" class="wide" label="なりすまし先のサービスアカウント（任意）"
            placeholder="vertex-user@project.iam.gserviceaccount.com" persistent-placeholder />
        </div>
        <v-btn variant="text" :prepend-icon="showGuide ? 'mdi-chevron-up' : 'mdi-chevron-down'" class="mt-2"
          @click="openGuide">GCP側の設定手順</v-btn>
        <div v-if="showGuide" class="guide">
          <v-alert v-if="ai.identityError.value" type="error" density="compact">{{ ai.identityError.value }}</v-alert>
          <template v-else-if="ai.identity.value">
            <dl>
              <dt>プレビューの身元（subject）</dt><dd><code>{{ ai.identity.value.subject }}</code></dd>
              <dt>発行元（issuer）</dt><dd><code>{{ ai.identity.value.issuer }}</code></dd>
            </dl>
            <v-btn variant="outlined" size="small" prepend-icon="mdi-download-outline" class="my-2"
              @click="downloadJwks">公開鍵（jwks.json）をダウンロード</v-btn>
            <pre class="commands">{{ commands }}</pre>
            <p class="meta">クラスタの鍵を入れ替えたときは、jwks.json を取り直してプロバイダーを更新してください。</p>
          </template>
          <v-progress-linear v-else indeterminate color="primary" />
        </div>
      </template>
    </template>
    <div v-if="ai.probe.value" class="tip my-4" :class="ai.probe.value.ok ? 'tip--brand' : 'tip--danger'"
      role="status">
      <v-icon :icon="ai.probe.value.ok ? 'mdi-check-circle-outline' : 'mdi-alert-circle-outline'" />
      <span v-if="ai.probe.value.ok">使えます。{{ ai.probe.value.model }} が「{{ ai.probe.value.text || '（空の応答）' }}」と
        答えました（{{ ((ai.probe.value.elapsed_ms ?? 0) / 1000).toFixed(1) }}秒）。</span>
      <span v-else>{{ stepLabels[ai.probe.value.step] ?? ai.probe.value.step }}で止まりました。
        <template v-if="ai.probe.value.status">（HTTP {{ ai.probe.value.status }}）</template>
        {{ ai.probe.value.message }}</span>
    </div>
    <v-card-actions class="actions">
      <v-btn variant="outlined" prepend-icon="mdi-connection" :loading="ai.testing.value || ai.saving.value"
        :disabled="ai.loading.value || keyBlocked || form.backend === 'none'" class="mr-auto"
        @click="saveAndTest">保存して接続をテスト</v-btn>
      <v-btn @click="emit('close')">キャンセル</v-btn>
      <v-btn color="primary" :loading="ai.saving.value" :disabled="ai.loading.value || keyBlocked" @click="save">
        保存する</v-btn>
    </v-card-actions>
  </v-card>
</template>

<style scoped>
.lead { color: var(--ink-3); }
/* 補足文のある欄に引っ張られて、隣の欄が縦に伸びないよう上で揃える。 */
.form { display: grid; grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr)); gap: var(--sp-3);
  margin-top: var(--sp-3); align-items: start; }
.form .wide { grid-column: 1 / -1; }
.guide { margin-top: var(--sp-3); padding: var(--sp-4); background: var(--surface-subtle);
  border: 1px solid var(--border-default); border-radius: var(--radius-md); }
.guide dl { display: grid; grid-template-columns: max-content 1fr; gap: var(--sp-1) var(--sp-3); font-size: var(--fs-sm); }
.guide dt { color: var(--ink-3); }
.guide code { overflow-wrap: anywhere; }
.commands { background: var(--surface-sunken); color: var(--ink-2); padding: var(--sp-3); margin: var(--sp-2) 0;
  border: 1px solid var(--border-subtle); border-radius: var(--radius-md); font-size: var(--fs-xs);
  white-space: pre-wrap; overflow-wrap: anywhere; }
.meta { color: var(--ink-4); font-size: var(--fs-xs); }
.actions { justify-content: flex-end; gap: var(--sp-2); }
</style>
