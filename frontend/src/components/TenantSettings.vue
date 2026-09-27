<script setup lang="ts">
import { onMounted, ref } from 'vue'
import SystemLlmSettings from '@/components/SystemLlmSettings.vue'
import TenantAiSettings from '@/components/TenantAiSettings.vue'
import { useTenantLlm } from '@/composables/useTenantLlm'
import type { Tenant } from '@/types'

// テナントの AI 設定。システム管理者と、そのテナントのテナント管理者が開く。
// 「生成に使うAI」は無ければシステムの既定を使う。「生成アプリが使う Gemini」はテナントだけの設定。
const props = defineProps<{ tenant: Tenant }>()
const emit = defineEmits<{ close: [] }>()
const llm = useTenantLlm(props.tenant.id)
const tab = ref<'generation' | 'apps'>('generation')
onMounted(llm.refresh)
</script>

<template>
  <v-card class="pa-6">
    <div class="head">
      <h2>{{ tenant.name }} の AI 設定</h2>
      <v-btn icon="mdi-close" variant="text" aria-label="閉じる" @click="emit('close')" />
    </div>
    <v-tabs v-model="tab" color="primary" density="comfortable" class="my-2 outer-tabs">
      <v-tab value="generation" prepend-icon="mdi-robot-outline">アプリの生成に使うAI</v-tab>
      <v-tab value="apps" prepend-icon="mdi-application-outline">生成したアプリが使う Gemini</v-tab>
    </v-tabs>
    <SystemLlmSettings v-if="tab === 'generation'" :tenant="llm" />
    <TenantAiSettings v-else :tenant="tenant" embedded @close="emit('close')" />
  </v-card>
</template>

<style scoped>
/* 製品名は大文字に変えない（Vuetify の既定は大文字）。 */
.outer-tabs :deep(.v-tab) { text-transform: none; letter-spacing: normal; }
.head { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-3); }
</style>
