<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '@/composables/useApi'
import type { RegistrySelection, RegistrySettings, RegistryIdentity } from '@/types'

const path = '/api/system/app-registry'
const state = ref<RegistrySettings>(), identity = ref<RegistryIdentity>()
const form = ref<RegistrySelection>({ kind: 'private', host: '', wif_project_number: '', wif_pool_id: '',
  wif_provider_id: '', writer_service_account: '', reader_service_account: '', scanning_enabled: false })
const busy = ref(false), error = ref(''), message = ref(''), showGuide = ref(false)
const identityError = ref(''), identityLoading = ref(false)
const artifact = computed(() => form.value.kind === 'artifact')
const composeRegistry = computed(() => state.value?.environment.kind === 'private' && state.value.environment.host.startsWith('localhost:'))
async function load() {
  try {
    state.value = await api<RegistrySettings>(path)
    if (state.value.selection) form.value = { ...state.value.selection }
    else if (state.value.environment.kind !== 'private') {
      form.value.kind = 'artifact'; form.value.host = state.value.environment.host
    }
  } catch (e) { error.value = e instanceof Error ? e.message : '設定を取得できません。' }
}
onMounted(load)
async function saveSettings() {
  const { password_configured: _configured, ...payload } = form.value
  const result = await api<{ selection: RegistrySelection }>(path, 'PUT', payload)
  form.value = { ...result.selection }
}
async function perform(action: 'save' | 'test') {
  busy.value = true; error.value = ''; message.value = ''
  try {
    if (action === 'save') {
      await saveSettings()
      message.value = '保存しました。次のビルドから使用します。'
    } else {
      await saveSettings()
      const result = await api<{ ok: boolean; message: string }>(`${path}/test`, 'POST', (({ password_configured: _configured, ...payload }) => payload)(form.value))
      if (result.ok) message.value = result.message
      else error.value = result.message
    }
  } catch (e) { error.value = e instanceof Error ? e.message : '設定を確認してください。' }
  finally { busy.value = false }
}
async function guide() {
  showGuide.value = !showGuide.value
  if (!showGuide.value || identity.value) return
  identityLoading.value = true; identityError.value = ''
  try { identity.value = await api<RegistryIdentity>(`${path}/workload-identity`) }
  catch (e) { identityError.value = e instanceof Error ? e.message : 'クラスタ情報を取得できません。' }
  finally { identityLoading.value = false }
}
function downloadJwks() {
  if (!identity.value) return
  const url = URL.createObjectURL(new Blob([JSON.stringify(identity.value.jwks, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a'); link.href = url; link.download = 'registry-jwks.json'; link.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000)
}
function quote(value: string) { return "'" + value.split("'").join("'\\''") + "'" }
const commands = computed(() => {
  const info = identity.value, f = form.value
  if (!info) return ''
  const [host, project, repository] = f.host.split('/')
  const region = (host || '<リージョン>-docker.pkg.dev').replace('-docker.pkg.dev', '')
  const pool = f.wif_pool_id || '<プールID>', provider = f.wif_provider_id || '<プロバイダーID>'
  const poolPath = `projects/${f.wif_project_number || '<プロジェクト番号>'}/locations/global/workloadIdentityPools/${pool}`
  const identities = [[info.writer_subject, f.writer_service_account, 'writer'],
    [info.reader_subject, f.reader_service_account, 'reader']]
  const sections = [
    ['# 1. 必要なAPIを有効にする',
      `gcloud services enable artifactregistry.googleapis.com iam.googleapis.com sts.googleapis.com${identities.some(([, account]) => account) ? ' iamcredentials.googleapis.com' : ''} --project=${quote(project || '<プロジェクトID>')}`].join('\n'),
    ['# 2. 生成アプリ用リポジトリ（未作成の場合のみ実行）',
      `gcloud artifacts repositories create ${quote(repository || '<リポジトリ>')} --repository-format=docker --project=${quote(project || '<プロジェクトID>')} --location=${quote(region)}`].join('\n'),
    ['# 3. WIFプール・プロバイダー（既存なら設定を確認・更新）',
      `POOL_PROJECT=$(gcloud projects describe ${quote(f.wif_project_number || '<プロジェクト番号>')} --format='value(projectId)')`,
      `gcloud iam workload-identity-pools create ${quote(pool)} --location=global --project="$POOL_PROJECT"`,
      `gcloud iam workload-identity-pools providers create-oidc ${quote(provider)} --location=global --project="$POOL_PROJECT" \\`,
      `  --workload-identity-pool=${quote(pool)} --issuer-uri=${quote(info.issuer)} --jwk-json-path=registry-jwks.json \\`,
      `  --allowed-audiences=${quote('https://iam.googleapis.com/' + poolPath + '/providers/' + provider)} \\`,
      `  --attribute-mapping=google.subject=assertion.sub \\`,
      `  --attribute-condition="assertion.sub in ['${info.writer_subject}','${info.reader_subject}']"`].join('\n'),
  ]
  identities.forEach(([subject, account, role], index) => {
    const principal = 'principal://iam.googleapis.com/' + poolPath + '/subject/' + subject
    const member = account ? 'serviceAccount:' + account : principal
    sections.push([
      `# ${index + 4}. ${role === 'writer' ? 'Push' : 'Pull'}用の権限（${account ? 'サービスアカウント経由' : 'Subjectへ直接付与'}）`,
      ...(account ? [
        `gcloud iam service-accounts add-iam-policy-binding ${quote(account)} --project=${quote(account.split('@')[1]?.replace('.iam.gserviceaccount.com', '') || project || '<プロジェクトID>')} \\`,
        `  --role=roles/iam.workloadIdentityUser --member=${quote(principal)}`,
      ] : []),
      `gcloud artifacts repositories add-iam-policy-binding ${quote(repository || '<リポジトリ>')} --project=${quote(project || '<プロジェクトID>')} --location=${quote(region)} \\`,
      `  --role=roles/artifactregistry.${role} --member=${quote(member)}`,
    ].join('\n'))
  })
  return sections.join('\n\n')
})
</script>

<template>
  <v-card title="生成アプリのイメージ保存先">
    <v-card-text>
      <p class="mb-4">オンプレ・GCEのどちらからでもArtifact Registryを利用できます。変更は次のビルドから適用し、公開中の版と過去の成果物は保持します。</p>
      <v-alert v-if="state && !state.enabled" type="info" class="mb-4">公開基盤をAnsibleで有効にしてから、接続確認とビルドを実行してください。</v-alert>
      <v-alert v-if="error" type="error" class="mb-4">{{ error }}</v-alert>
      <v-alert v-if="message" type="success" class="mb-4">{{ message }}</v-alert>
      <v-select v-model="form.kind" label="保存先" :items="[{ title: '内部Registry', value: 'private', props: { disabled: state?.environment.kind !== 'private' } }, { title: 'Artifact Registry（WIF）', value: 'artifact', props: { disabled: composeRegistry } }]" />
      <template v-if="!artifact">
        <p v-if="composeRegistry" class="meta">Compose版はローカルRegistryを使います。接続先・ユーザー名・パスワードは空欄のままで利用できます。</p>
        <div class="form">
          <v-text-field v-model="form.host" class="wide" label="内部Registryの接続先" :placeholder="state?.environment.host" hint="ホスト名:ポート。空欄ならAnsibleの接続設定を使用します。" persistent-hint />
          <v-text-field v-model="form.username" label="ユーザー名" autocomplete="off" />
          <v-text-field v-model="form.password" type="password" label="パスワード" autocomplete="new-password" :hint="form.password_configured ? '登録済み。空欄なら保存済みのパスワードを保持します。' : '接続先を指定する場合は入力してください。'" persistent-hint />
        </div>
        <v-checkbox v-model="form.http" label="HTTPで接続する" />
        <p v-if="!composeRegistry" class="meta">Registry側のユーザー・パスワード設定、ノードの名前解決、接続先への通信許可、保存領域はAnsibleで構成してください。HTTP接続の指定は、接続テスト・公開時に全ノードのPull設定へ自動反映します。接続先の変更はAnsibleの配備設定と揃えてください。ここでRegistryのユーザーは作成されません。</p>
      </template>
      <template v-else>
        <div class="form">
          <v-text-field v-model="form.host" class="wide" label="Artifact Registryのリポジトリ" placeholder="asia-northeast1-docker.pkg.dev/project-id/koyorina-apps" />
        </div>
        <h3 class="section">Workload Identity 連携（鍵ファイルを使わずに認証します）</h3>
        <div class="form">
          <v-text-field v-model="form.wif_project_number" label="プールのプロジェクト番号" />
          <v-text-field v-model="form.wif_pool_id" label="プールID" />
          <v-text-field v-model="form.wif_provider_id" label="プロバイダーID" />
          <v-text-field v-model="form.writer_service_account" class="wide" label="Push用のなりすまし先サービスアカウント（任意）" placeholder="builder@project-id.iam.gserviceaccount.com" />
          <v-text-field v-model="form.reader_service_account" class="wide" label="Pull用のなりすまし先サービスアカウント（任意）" placeholder="reader@project-id.iam.gserviceaccount.com" />
        </div>
        <p class="meta">空欄なら各Kubernetes ServiceAccountのSubjectにArtifact Registryの権限を直接付与します。なりすまし方式を使う場合だけGoogle Cloudサービスアカウントを指定してください。両方に指定する場合は別々のアカウントを使います。</p>
        <v-checkbox v-model="form.scanning_enabled" label="GCPで脆弱性検査を有効化済み" />
        <v-btn variant="text" class="mt-3" :append-icon="showGuide ? 'mdi-chevron-up' : 'mdi-chevron-down'" @click="guide">GCP側の設定手順</v-btn>
        <div v-if="showGuide" class="guide">
          <v-progress-linear v-if="identityLoading" indeterminate />
          <v-alert v-else-if="identityError" type="error" density="compact">{{ identityError }}</v-alert>
          <template v-else-if="identity">
            <p>公開鍵をダウンロードし、GCP側で信頼するクラスタの身元とIAM権限を設定します。</p>
            <dl>
              <dt>PushのSubject</dt><dd><code>{{ identity.writer_subject }}</code></dd>
              <dt>PullのSubject</dt><dd><code>{{ identity.reader_subject }}</code></dd>
              <dt>Issuer</dt><dd><code>{{ identity.issuer }}</code></dd>
            </dl>
            <v-btn variant="outlined" size="small" prepend-icon="mdi-download-outline" class="my-3" @click="downloadJwks">公開鍵（JWKS）をダウンロード</v-btn>
            <pre class="commands">{{ commands }}</pre>
            <p class="meta">クラスタの署名鍵を更新した場合は、プロバイダーのJWKSも更新してください。</p>
          </template>
        </div>
      </template>
    </v-card-text>
    <v-card-actions>
      <v-btn variant="outlined" prepend-icon="mdi-connection" class="mr-auto" :disabled="busy || !state?.enabled" @click="perform('test')">保存して接続をテスト</v-btn>
      <v-btn color="primary" :loading="busy" @click="perform('save')">保存する</v-btn>
    </v-card-actions>
  </v-card>
</template>

<style scoped>
 .form { display: grid; grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr)); gap: var(--sp-3); margin-top: var(--sp-3); align-items: start; }
.form .wide { grid-column: 1 / -1; }
.section { margin-top: var(--sp-5); font-size: var(--fs-sm); font-weight: var(--fw-medium); color: var(--ink-2); }
.guide { margin-top: var(--sp-3); padding: var(--sp-4); background: var(--surface-subtle); border: 1px solid var(--border-default); border-radius: var(--radius-md); }
.guide dl { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: var(--sp-1) var(--sp-3); font-size: var(--fs-sm); margin-top: var(--sp-3); }
.guide dt { color: var(--ink-3); }
.guide code { overflow-wrap: anywhere; }
.commands { background: var(--surface-sunken); color: var(--ink-2); padding: var(--sp-3); margin: var(--sp-2) 0; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); font-size: var(--fs-xs); white-space: pre-wrap; overflow-wrap: anywhere; }
.meta { color: var(--ink-4); font-size: var(--fs-xs); }
</style>
