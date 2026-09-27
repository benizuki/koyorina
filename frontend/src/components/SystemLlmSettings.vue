<script setup lang="ts">
import { ref } from 'vue'
import SystemGemini from '@/components/SystemGemini.vue'
import SystemLlmProvider from '@/components/SystemLlmProvider.vue'
import type { TenantLlm } from '@/composables/useTenantLlm'

// 生成AIごとにタブを分ける。どのタブも「保存するまでは環境の設定のまま」で揃えている。
// tenant を渡すと、そのテナントの設定（無ければシステムの既定を使う）を扱う。
defineProps<{ tenant?: TenantLlm }>()
const tab = ref<'gemini' | 'antigravity' | 'openai-compatible' | 'claude'>('gemini')
</script>

<template>
  <v-card :class="{ 'pa-6': !tenant }" :border="!tenant" :flat="!!tenant">
    <h2 v-if="!tenant">生成AIの設定</h2>
    <v-tabs v-model="tab" color="primary" density="comfortable" class="my-3 llm-tabs">
      <v-tab value="gemini" prepend-icon="mdi-google">Gemini</v-tab>
      <v-tab value="antigravity" prepend-icon="mdi-rocket-launch-outline">Antigravity</v-tab>
      <v-tab value="openai-compatible" prepend-icon="mdi-api">OpenAI互換API</v-tab>
      <v-tab value="claude" prepend-icon="mdi-asterisk">Claude</v-tab>
    </v-tabs>
    <SystemGemini v-if="tab === 'gemini'" :tenant="tenant" />
    <SystemLlmProvider v-else :key="tab" :kind="tab" :tenant="tenant" />
  </v-card>
</template>

<style scoped>
/* 製品名は大文字に変えない（Vuetify の既定は大文字）。 */
.llm-tabs :deep(.v-tab) { text-transform: none; letter-spacing: normal; }
</style>
