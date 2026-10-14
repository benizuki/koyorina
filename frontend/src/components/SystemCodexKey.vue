<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useSystemLlm } from '@/composables/useSystemLlm'

// Codexの認証方式。各自のChatGPTか、運営の共通APIキー（OpenAI）か。システム全体で1つ。
// 保存は enabled（true=共通APIキー）。各自の認証へ戻してもキーは残り、再び切り替えるときに入れ直さなくてよい。
const llm = useSystemLlm('codex')
const mode = ref<'individual' | 'shared'>('individual'), apiKey = ref('')
watch(llm.current, value => {
  if (value) mode.value = value.saved && value.enabled ? 'shared' : 'individual'
}, { immediate: true })
onMounted(llm.refresh)
const keyMissing = computed(() => mode.value === 'shared' && !apiKey.value.trim()
  && !llm.current.value?.api_key_configured)
async function save() {
  if (await llm.save({ enabled: mode.value === 'shared' }, apiKey.value.trim())) apiKey.value = ''
}
</script>

<template>
  <div>
    <p class="lead">Codex をどの認証で使うかを選びます。</p>
    <v-alert v-if="llm.error.value" type="error" density="compact" class="my-3">{{ llm.error.value }}</v-alert>
    <v-progress-linear v-if="llm.loading.value" indeterminate color="primary" class="my-3" />
    <template v-else-if="llm.current.value">
      <v-radio-group v-model="mode" hide-details class="my-2" aria-label="Codex の認証方式">
        <v-radio value="individual" label="各自の ChatGPT アカウントで認証する（各自の利用枠を使います）" />
        <v-radio value="shared" label="運営の共通APIキーを使う（全員がこのキーで生成し、利用料はキーの組織に請求されます）" />
      </v-radio-group>
      <template v-if="mode === 'shared'">
        <p v-if="apiKey && !llm.current.value.secrets_available" class="tip tip--warn my-3">
          <v-icon icon="mdi-alert-outline" />
          <span>APIキーを暗号化する鍵（TENANT_SECRET_KEY）が設定されていないため、キーを保存できません。
            運用担当者に設定を依頼してください。</span></p>
        <v-text-field v-model="apiKey" type="password" autocomplete="off" class="my-3"
          label="OpenAI の APIキー"
          :placeholder="llm.current.value.api_key_configured ? '設定済み（入れ替えるときだけ入力）' : 'sk-...'"
          persistent-placeholder />
      </template>
      <p class="meta mt-4">キーは利用者の画面には表示しません。切り替えても、各自の ChatGPT のログインは消えません。
        生成の実行環境は、使っていないものはすぐに、生成中のものは終わりしだい入れ替わります。</p>
      <p v-if="llm.current.value.agents_synced === false" class="tip tip--warn mt-3">
        <v-icon icon="mdi-alert-outline" /><span>設定は保存しましたが、生成エージェントの管理（codex-controller）へ
          届きませんでした。少し待ってからもう一度保存してください。</span></p>
      <div class="actions">
        <v-btn color="primary" :loading="llm.saving.value"
          :disabled="keyMissing || (!!apiKey && !llm.current.value.secrets_available)" @click="save">保存する</v-btn>
      </div>
    </template>
  </div>
</template>

<style scoped>
.lead { color: var(--ink-3); }
.meta { color: var(--ink-4); font-size: var(--fs-xs); }
.actions { display: flex; justify-content: flex-end; gap: var(--sp-2); margin-top: var(--sp-4); }
</style>
