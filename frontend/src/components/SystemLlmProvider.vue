<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useSystemLlm, type SystemLlmKind } from '@/composables/useSystemLlm'
import { tenantProvider, type TenantLlm } from '@/composables/useTenantLlm'
import type { TenantLlmMode } from '@/types'

// 生成に使う Antigravity／OpenAI互換API／Claude。保存するまでは環境の設定のまま動く。
// tenant を渡すと、そのテナントの設定にする（無ければシステムの既定を使う）。
const props = defineProps<{ kind: SystemLlmKind; tenant?: TenantLlm }>()
const llm = props.tenant ? tenantProvider(props.tenant, props.kind) : useSystemLlm(props.kind)
const tenantKey = props.kind === 'openai-compatible' ? 'openai_compatible' : props.kind
const entry = computed(() => props.tenant?.state.value?.[tenantKey])
const mode = ref<TenantLlmMode>('system')
watch(() => entry.value?.mode, value => { if (value) mode.value = value }, { immediate: true })
const editable = computed(() => !props.tenant || mode.value === 'tenant')
const synced = computed(() => props.tenant ? entry.value?.agents_synced : llm.current.value?.agents_synced)
const systemSummary = computed(() => {
  const value = entry.value?.system
  if (!value) return ''
  if (!value.enabled) return value.saved ? '無効' : `環境の設定（${environmentSummary.value}）`
  if (claude.value) return `有効（${value.backend === 'vertex' ? 'Vertex AI' : 'APIキー'}／モデル：${value.model}）`
  return antigravity.value ? `有効（モデル：${value.model}）` : `有効（${value.label}／${value.base_url}／モデル：${value.model}）`
})
const antigravity = computed(() => props.kind === 'antigravity')
const claude = computed(() => props.kind === 'claude')
const kindName = computed(() => claude.value ? 'Claude' : antigravity.value ? 'Antigravity' : 'OpenAI互換API')
const backend = ref<'api_key' | 'vertex'>('api_key'), project = ref(''), location = ref('global')
const claudeModels = ['claude-sonnet-5', 'claude-opus-5-5']
const enabled = ref(false), model = ref(''), agent = ref(''), maxTokens = ref(50000)
const label = ref(''), baseUrl = ref(''), apiKey = ref('')
const antigravityModels = ['gemini-3.8-flash', 'gemini-3.5-flash']

watch(llm.current, value => {
  if (!value) return
  // 保存前は、いま使っている環境の設定を初期値にする。
  const source = value.saved ? value : { ...value.environment, agent: '', max_total_tokens: 50000 }
  enabled.value = source.enabled
  model.value = source.model || (antigravity.value ? 'gemini-3.8-flash' : claude.value ? 'claude-sonnet-5' : '')
  backend.value = ('backend' in source && source.backend === 'vertex') ? 'vertex' : 'api_key'
  project.value = ('gcp_project' in source && source.gcp_project) || ''
  location.value = ('location' in source && source.location) || 'global'
  agent.value = ('agent' in source && source.agent) || 'antigravity-preview-09-2026'
  maxTokens.value = ('max_total_tokens' in source && source.max_total_tokens) || 50000
  label.value = source.label || 'OpenAI互換API'
  baseUrl.value = source.base_url || ''
}, { immediate: true })
onMounted(() => { if (!props.tenant) llm.refresh() })

// OpenAI互換APIはキーが要る。Antigravity は入れなければ環境の Gemini API キーを使う。
const keyBlocked = computed(() => !!apiKey.value && !llm.current.value?.secrets_available)
const environmentSummary = computed(() => {
  const env = llm.current.value?.environment
  if (!env) return ''
  if (!env.enabled) return '無効'
  return antigravity.value ? `有効（モデル：${env.model || '未設定'}）`
    : `有効（${env.label || '表示名なし'}／${env.base_url || '接続先未設定'}／モデル：${env.model || '未設定'}）`
})
async function save() {
  if (props.tenant && mode.value !== 'tenant') return props.tenant.save(tenantKey, mode.value)
  const input = antigravity.value
    ? { enabled: enabled.value, model: model.value, agent: agent.value,
        max_total_tokens: Number(maxTokens.value) }
    : claude.value
      ? { enabled: enabled.value, backend: backend.value, model: model.value,
          gcp_project: backend.value === 'vertex' ? project.value : '',
          location: backend.value === 'vertex' ? location.value : '' }
      : { enabled: enabled.value, label: label.value, base_url: baseUrl.value, model: model.value }
  const key = claude.value && backend.value === 'vertex' ? '' : apiKey.value.trim()
  if (await llm.save(input, key)) apiKey.value = ''
}
</script>

<template>
  <div>
    <p class="lead">
      <template v-if="antigravity">Google 管理の Remote Sandbox でアプリを生成する経路です。有効にすると、
        開発画面のモデルの選択肢に Antigravity が出ます。</template>
      <template v-else-if="claude">Claude（Claude Agent SDK）でアプリを生成する経路です。Claude on Vertex AI か
        Anthropic の APIキーで使います。Gemini と同じく、ファイルの読み書きだけを任せ、コマンドは実行させません。</template>
      <template v-else>OpenAI の API と同じ形で呼べる LLM（社内のLLMサーバーなど）でアプリを生成する経路です。
        有効にすると、開発画面のモデルの選択肢に表示名で出ます。</template>
    </p>
    <v-alert v-if="llm.error.value" type="error" density="compact" class="my-3">{{ llm.error.value }}</v-alert>
    <v-progress-linear v-if="llm.loading.value" indeterminate color="primary" class="my-3" />
    <template v-else-if="llm.current.value">
      <v-radio-group v-if="tenant" v-model="mode" inline hide-details class="my-2" aria-label="このテナントでの扱い">
        <v-radio label="システムの既定を使う" value="system" />
        <v-radio label="このテナントの設定を使う" value="tenant" />
        <v-radio label="使わせない" value="disabled" />
      </v-radio-group>
      <p v-if="tenant && mode === 'system'" class="tip my-3">
        <v-icon icon="mdi-information-outline" /><span>システムの既定：{{ systemSummary }}</span></p>
      <p v-if="tenant && mode === 'disabled'" class="tip tip--warn my-3">
        <v-icon icon="mdi-cancel" /><span>このテナントでは選べなくなります。</span></p>
      <template v-if="editable">
      <p v-if="!tenant && !llm.current.value.saved" class="tip my-3">
        <v-icon icon="mdi-information-outline" />
        <span>まだ保存していないため、環境の設定を使っています：{{ environmentSummary }}</span></p>
      <v-switch v-model="enabled" color="primary" hide-details inset class="my-2"
        :label="`${kindName} を使えるようにする`" />
      <template v-if="enabled">
        <div v-if="antigravity" class="form">
          <v-combobox v-model="model" :items="antigravityModels" label="モデル" />
          <v-text-field v-model="agent" label="エージェント名" />
          <v-text-field v-model.number="maxTokens" type="number" min="1000" max="200000"
            label="1回の生成で使う上限（トークン）" />
        </div>
        <template v-else-if="claude">
          <v-radio-group v-model="backend" inline hide-details class="my-2" aria-label="Claude の接続先">
            <v-radio label="Anthropic の APIキー" value="api_key" />
            <v-radio label="Claude on Vertex AI" value="vertex" />
          </v-radio-group>
          <div class="form">
            <v-combobox v-model="model" :items="claudeModels" label="モデル" />
            <template v-if="backend === 'vertex'">
              <v-text-field v-model="project" label="GCP プロジェクトID" />
              <v-text-field v-model="location" label="リージョン" placeholder="global や us-east5"
                persistent-placeholder />
            </template>
          </div>
          <p v-if="backend === 'vertex'" class="meta">認証は「Gemini」タブの Vertex AI（Workload Identity 連携か
            鍵ファイル）と同じ身元を使います。その身元に、上のプロジェクトで Claude を使う権限
            （roles/aiplatform.user）を付けてください。Claude のモデルは Model Garden で有効にしておく必要があります。</p>
        </template>
        <div v-else class="form">
          <v-text-field v-model="label" label="表示名" placeholder="例：社内LLM" maxlength="40" persistent-placeholder />
          <v-text-field v-model="model" label="モデル" placeholder="例：qwen3-coder" persistent-placeholder />
          <v-text-field v-model="baseUrl" class="wide" label="接続先URL"
            placeholder="https://llm.example.com/v1" persistent-placeholder />
        </div>
        <p v-if="apiKey && keyBlocked" class="tip tip--warn my-3">
          <v-icon icon="mdi-alert-outline" />
          <span>APIキーを暗号化する鍵（TENANT_SECRET_KEY）が設定されていないため、キーを保存できません。
            運用担当者に設定を依頼してください。</span></p>
        <v-text-field v-if="!(claude && backend === 'vertex')" v-model="apiKey" type="password" autocomplete="off"
          class="my-3" :label="antigravity ? 'Gemini API のAPIキー（任意）' : claude ? 'Anthropic の APIキー' : 'APIキー'"
          :placeholder="llm.current.value.api_key_configured ? '設定済み（入れ替えるときだけ入力）'
            : antigravity ? '入れなければ、環境に設定された Gemini API のキーを使います' : ''"
          persistent-placeholder />
        <p v-if="!antigravity && !claude" class="meta">生成エージェントの実行環境から接続先へ届く必要があります。
          社内のサーバーを使う場合は、ネットワークの通り道（NetworkPolicy）を運用担当者に確認してください。</p>
      </template>
      </template>
      <p class="meta mt-4">開発画面の選択肢は保存した直後から変わります。生成の実行環境は、使っていないものはすぐに、
        生成中のものは終わりしだい入れ替わります（入れ替えた直後の最初の依頼は、起動を待つぶん少し時間がかかります）。</p>
      <p v-if="synced === false" class="tip tip--warn mt-3">
        <v-icon icon="mdi-alert-outline" /><span>設定は保存しましたが、生成エージェントの管理（codex-controller）へ
          届きませんでした。少し待ってからもう一度保存してください。</span></p>
      <div class="actions">
        <v-btn color="primary" :loading="llm.saving.value" :disabled="editable && keyBlocked" @click="save">保存する</v-btn>
      </div>
    </template>
  </div>
</template>

<style scoped>
.lead { color: var(--ink-3); }
.form { display: grid; grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr)); gap: var(--sp-3);
  margin-top: var(--sp-3); align-items: start; }
.form .wide { grid-column: 1 / -1; }
.meta { color: var(--ink-4); font-size: var(--fs-xs); }
.actions { display: flex; justify-content: flex-end; gap: var(--sp-2); margin-top: var(--sp-4); }
</style>
