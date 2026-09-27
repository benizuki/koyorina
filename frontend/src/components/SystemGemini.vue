<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useSystemSettings, type SystemGeminiForm } from '@/composables/useSystemSettings'
import { tenantGemini, type TenantLlm } from '@/composables/useTenantLlm'
import type { SystemGemini, TenantLlmMode } from '@/types'

// 本体（PDF読取・目的の下書き・音声入力）と、Geminiでの生成・ヒアリングが使う Gemini。
// Gemini API（APIキー）か Vertex AI（Workload Identity 連携）の2択。保存するまでは環境の設定を使う。
// tenant を渡すと、そのテナントの生成で使う Gemini を設定する（無ければシステムの既定を使う）。
const props = defineProps<{ tenant?: TenantLlm }>()
const system = props.tenant ? tenantGemini(props.tenant) : useSystemSettings()
const mode = ref<TenantLlmMode>('system')
watch(() => props.tenant?.state.value?.gemini.mode, value => { if (value) mode.value = value }, { immediate: true })
const editable = computed(() => !props.tenant || mode.value === 'tenant')
const synced = computed(() => props.tenant ? props.tenant.state.value?.gemini.agents_synced
  : system.gemini.value?.agents_synced)
const form = ref<SystemGeminiForm>({ backend: 'vertex', gcp_project: '', location: 'global', model: 'gemini-3.5-flash',
  thinking_level: '', wif_project_number: '', wif_pool_id: '', wif_provider_id: '', wif_service_account: '' })
const apiKey = ref(''), showGuide = ref(false)
const modelChoices = ['gemini-3.8-flash', 'gemini-3.5-flash']
const locationChoices = ['global', 'us-central1', 'asia-northeast1', 'europe-west4']
const thinkingChoices = [{ title: '指定しない（モデルの既定）', value: '' }, { title: 'MINIMAL', value: 'MINIMAL' },
  { title: 'LOW', value: 'LOW' }, { title: 'MEDIUM', value: 'MEDIUM' }, { title: 'HIGH', value: 'HIGH' }]

watch(system.gemini, value => {
  if (!value) return
  const { api_key_configured: _, secrets_available: __, environment, agents_synced: ___, wif_enabled: ____,
    backend, ...rest } = value as SystemGemini & { wif_enabled?: boolean }
  // 保存前は、いま使っている環境の設定に近いほうを選んでおく。
  form.value = { ...rest, backend: backend === 'env' ? environment.backend : backend,
    gcp_project: rest.gcp_project || (backend === 'env' ? environment.gcp_project : ''),
    location: rest.location || 'global', model: rest.model || environment.model || 'gemini-3.5-flash' }
}, { immediate: true })
onMounted(() => { if (!props.tenant) system.refresh() })

const vertex = computed(() => form.value.backend === 'vertex')
const geminiApi = computed(() => form.value.backend === 'gemini_api')
const keyBlocked = computed(() => geminiApi.value && !system.gemini.value?.secrets_available)
const unsaved = computed(() => !props.tenant && system.gemini.value?.backend === 'env')
// テナントの画面で見せる、システムの既定の要約。
const systemSummary = computed(() => {
  const value = props.tenant?.state.value?.gemini.system
  if (!value) return ''
  if (value.backend === 'env') return `環境の設定（${environmentSummary.value}）`
  return value.backend === 'gemini_api' ? `Gemini API（モデル：${value.model}）`
    : `Vertex AI（${value.gcp_project}／${value.location}、モデル：${value.model}）`
})
const environmentSummary = computed(() => {
  const env = system.gemini.value?.environment
  if (!env) return ''
  return env.backend === 'gemini_api' ? `Gemini API（モデル：${env.model || '未設定'}）`
    : `Vertex AI（${env.gcp_project || '未設定'}／${env.location || '未設定'}、モデル：${env.model || '未設定'}、`
      + '認証：鍵ファイル）'
})
const poolPath = computed(() => `projects/${form.value.wif_project_number || '<プロジェクト番号>'}/locations/global/`
  + `workloadIdentityPools/${form.value.wif_pool_id || '<プールID>'}`)
const commands = computed(() => {
  const info = system.identity.value
  if (!info) return ''
  const pool = form.value.wif_pool_id || '<プールID>', provider = form.value.wif_provider_id || '<プロバイダーID>'
  const poolNumber = form.value.wif_project_number || '<プールを置くプロジェクトの番号>'
  const project = form.value.gcp_project || '<Vertex AI を使うプロジェクトID>'
  const members = [info.app_subject, info.agent_subject].filter(Boolean)
    .map(subject => `principal://iam.googleapis.com/${poolPath.value}/subject/${subject}`)
  const target = form.value.wif_service_account
  return [
    '# 1. プールとOIDCプロバイダーを作る（テナント用と同じプールを使ってもよい）',
    '# プロジェクト番号からプロジェクトIDを引く（workload-identity-pools は ID しか受け付けない）',
    `POOL_PROJECT=$(gcloud projects describe ${poolNumber} --format='value(projectId)')`,
    `gcloud iam workload-identity-pools create ${pool} --location=global --project="$POOL_PROJECT"`,
    `gcloud iam workload-identity-pools providers create-oidc ${provider} --location=global \\`,
    `  --workload-identity-pool=${pool} --project="$POOL_PROJECT" \\`,
    `  --issuer-uri=${info.issuer} --jwk-json-path=jwks.json \\`,
    '  --attribute-mapping=google.subject=assertion.sub',
    '',
    target
      ? `# 2. ${who.value}に、サービスアカウントへのなりすましを許す`
      : `# 2. ${who.value}に、Vertex AI の利用を許す（Vertex AI を使うプロジェクト）`,
    ...members.map(member => target
      ? `gcloud iam service-accounts add-iam-policy-binding ${target} --role=roles/iam.workloadIdentityUser \\\n  --member=${member}`
      : `gcloud projects add-iam-policy-binding ${project} --role=roles/aiplatform.user \\\n  --member=${member}`),
  ].join('\n')
})

const who = computed(() => props.tenant ? 'このテナントの生成エージェントの身元' : '本体と生成エージェントの2つの身元')

async function openGuide() {
  showGuide.value = !showGuide.value
  if (showGuide.value && !system.identity.value) await system.loadIdentity()
}
function downloadJwks() {
  if (!system.identity.value) return
  const url = URL.createObjectURL(new Blob([JSON.stringify(system.identity.value.jwks, null, 2)],
    { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url; link.download = 'jwks.json'; link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000)
}
async function persist() {
  if (props.tenant && mode.value !== 'tenant') return props.tenant.save('gemini', mode.value)
  const ok = await system.save(form.value, geminiApi.value ? apiKey.value.trim() : '')
  if (ok) apiKey.value = ''
  return ok
}
async function saveAndTest() { if (await persist()) await system.test() }
const stepLabels: Record<string, string> = {
  credentials: 'Kubernetes のトークンの取得', sts: 'トークンの交換（STS）', generate: 'Gemini への問い合わせ',
}
</script>

<template>
  <div>
    <p v-if="tenant" class="lead">このテナントのアプリを Gemini で生成するとき（ヒアリングを含む）に使う接続先です。
      設定しなければシステムの既定を使います。</p>
    <p v-else class="lead">PDF読取・目的の下書き・音声入力と、Gemini での生成・ヒアリングが使う Gemini です。
      Gemini API（APIキー）か Vertex AI（Workload Identity 連携）を選びます。テナントごとに別の接続先を使うときは、
      テナントの「AI設定」で設定します。</p>
    <v-alert v-if="system.error.value" type="error" density="compact" class="my-3">{{ system.error.value }}</v-alert>
    <v-progress-linear v-if="system.loading.value" indeterminate color="primary" class="my-3" />
    <template v-else-if="system.gemini.value">
      <v-radio-group v-if="tenant" v-model="mode" inline hide-details class="my-2" aria-label="このテナントでの扱い">
        <v-radio label="システムの既定を使う" value="system" />
        <v-radio label="このテナントの設定を使う" value="tenant" />
        <v-radio label="使わせない" value="disabled" />
      </v-radio-group>
      <p v-if="tenant && mode === 'system'" class="tip my-3">
        <v-icon icon="mdi-information-outline" /><span>システムの既定：{{ systemSummary }}</span></p>
      <p v-if="tenant && mode === 'disabled'" class="tip tip--warn my-3">
        <v-icon icon="mdi-cancel" /><span>このテナントでは Gemini での生成とヒアリングを選べなくなります。</span></p>
      <template v-if="editable">
      <v-radio-group v-model="form.backend" inline hide-details class="my-2" aria-label="使う Gemini">
        <v-radio label="Gemini API（APIキー）" value="gemini_api" />
        <v-radio label="Vertex AI（Workload Identity 連携）" value="vertex" />
      </v-radio-group>
      <p v-if="unsaved" class="tip my-3">
        <v-icon icon="mdi-information-outline" />
        <span>まだ保存していないため、環境の設定を使っています：{{ environmentSummary }}</span></p>

      <div class="form">
        <v-combobox v-model="form.model" :items="modelChoices" label="モデル" />
        <v-select v-model="form.thinking_level" :items="thinkingChoices" label="思考レベル" />
      </div>

      <template v-if="geminiApi">
        <p v-if="keyBlocked" class="tip tip--warn my-3">
          <v-icon icon="mdi-alert-outline" />
          <span>APIキーを暗号化する鍵（TENANT_SECRET_KEY）が設定されていないため、保存できません。
            運用担当者に設定を依頼するか、Vertex AI を選んでください。</span></p>
        <v-text-field v-model="apiKey" type="password" autocomplete="off" label="Gemini API のAPIキー"
          :placeholder="system.gemini.value.api_key_configured ? '設定済み（入れ替えるときだけ入力）' : ''"
          :disabled="keyBlocked" persistent-placeholder class="my-3" />
      </template>

      <template v-if="vertex">
        <div class="form">
          <v-text-field v-model="form.gcp_project" label="Vertex AI を使うGCPプロジェクトID" />
          <v-combobox v-model="form.location" :items="locationChoices" label="リージョン" />
        </div>
        <h3 class="section">Workload Identity 連携（鍵ファイルを使わずに認証します）</h3>
        <div class="form">
          <v-text-field v-model="form.wif_project_number" label="プールのプロジェクト番号" />
          <v-text-field v-model="form.wif_pool_id" label="プールID" />
          <v-text-field v-model="form.wif_provider_id" label="プロバイダーID" />
          <v-text-field v-model="form.wif_service_account" class="wide"
            label="なりすまし先のサービスアカウント（任意）" placeholder="koyorina-vertex@project.iam.gserviceaccount.com"
            persistent-placeholder />
        </div>
        <v-btn variant="text" :prepend-icon="showGuide ? 'mdi-chevron-up' : 'mdi-chevron-down'" class="mt-3"
          @click="openGuide">GCP側の設定手順</v-btn>
        <div v-if="showGuide" class="guide">
          <v-alert v-if="system.identityError.value" type="error" density="compact">{{ system.identityError.value }}</v-alert>
          <template v-else-if="system.identity.value">
            <dl>
              <dt>本体の身元</dt><dd><code>{{ system.identity.value.app_subject }}</code></dd>
              <dt>生成エージェントの身元</dt><dd><code>{{ system.identity.value.agent_subject }}</code></dd>
              <dt>発行元（issuer）</dt><dd><code>{{ system.identity.value.issuer }}</code></dd>
            </dl>
            <v-btn variant="outlined" size="small" prepend-icon="mdi-download-outline" class="my-2"
              @click="downloadJwks">公開鍵（jwks.json）をダウンロード</v-btn>
            <pre class="commands">{{ commands }}</pre>
          </template>
          <v-progress-linear v-else indeterminate color="primary" />
        </div>
      </template>

      </template>

      <p class="meta mt-4"><template v-if="!tenant">本体は保存した直後から新しい設定を使います。</template>生成の実行環境は、使っていないものはすぐに、
        生成中のものは終わりしだい入れ替わります（入れ替えた直後の最初の依頼は、起動を待つぶん少し時間がかかります）。</p>
      <p v-if="synced === false" class="tip tip--warn mt-3">
        <v-icon icon="mdi-alert-outline" /><span>設定は保存しましたが、生成エージェントの管理（codex-controller）へ
          届きませんでした。少し待ってからもう一度保存してください。</span></p>

      <div v-if="system.probe.value" class="tip my-4" :class="system.probe.value.ok ? 'tip--brand' : 'tip--danger'"
        role="status">
        <v-icon :icon="system.probe.value.ok ? 'mdi-check-circle-outline' : 'mdi-alert-circle-outline'" />
        <span v-if="system.probe.value.ok">使えます。{{ system.probe.value.model }} が「{{ system.probe.value.text || '（空の応答）' }}」と答えました。</span>
        <span v-else>{{ stepLabels[system.probe.value.step] ?? system.probe.value.step }}で止まりました。
          {{ system.probe.value.message }}</span>
      </div>
      <div class="actions">
        <v-btn v-if="!tenant" variant="outlined" prepend-icon="mdi-connection" class="mr-auto"
          :loading="system.testing.value" :disabled="system.saving.value || keyBlocked" @click="saveAndTest">
          保存して接続をテスト</v-btn>
        <v-btn color="primary" :loading="system.saving.value" :disabled="editable && keyBlocked"
          @click="persist">保存する</v-btn>
      </div>
    </template>
  </div>
</template>

<style scoped>
.lead { color: var(--ink-3); }
.section { margin-top: var(--sp-5); font-size: var(--fs-sm); font-weight: var(--fw-medium); color: var(--ink-2); }
.form { display: grid; grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr)); gap: var(--sp-3);
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
.actions { display: flex; justify-content: flex-end; gap: var(--sp-2); margin-top: var(--sp-4); }
</style>
