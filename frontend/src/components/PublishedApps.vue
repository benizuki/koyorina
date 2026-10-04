<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { api } from '@/composables/useApi'
import type { PublishedApp } from '@/types'
const apps = ref<PublishedApp[]>([]), error = ref(''), loading = ref(false)
const tenant = ref('')
const tenants = computed(() => [...new Map(apps.value.map(app => [app.tenant_id,
  { title: app.tenant_name, value: app.tenant_id }])).values()])
const visible = computed(() => apps.value.filter(app => !tenant.value || app.tenant_id === tenant.value))
async function refresh() {
  loading.value = true; error.value = ''
  try { apps.value = await api<PublishedApp[]>('/api/published-apps') }
  catch (e) { error.value = e instanceof Error ? e.message : 'アプリ一覧を取得できません。' }
  finally { loading.value = false }
}
watch(tenants, options => {
  if (tenant.value && !options.some(option => option.value === tenant.value)) tenant.value = ''
})
onMounted(refresh)
</script>
<template>
  <section aria-labelledby="published-apps-title">
    <div class="d-flex align-center justify-space-between mb-5">
      <h1 id="published-apps-title">利用できるアプリ</h1>
      <v-btn :loading="loading" variant="outlined" prepend-icon="mdi-refresh" @click="refresh">一覧を更新</v-btn>
    </div>
    <v-alert v-if="error" type="error" variant="tonal">{{ error }}</v-alert>
    <v-select v-if="apps.length" v-model="tenant"
      :items="[{ title: 'すべてのテナント', value: '' }, ...tenants]"
      label="テナント" hide-details class="mb-5" style="max-width: 22rem" />
    <v-card v-if="!loading && !apps.length" class="pa-8 text-center">
      <v-icon icon="mdi-apps" size="x-large" color="primary" class="mb-4" />
      <p>現在利用できる公開アプリはありません。</p>
    </v-card>
    <div v-else class="app-grid">
      <v-card v-for="app in visible" :key="app.id" class="pa-5 app-card">
        <v-icon icon="mdi-application-outline" color="primary" size="large" class="mb-4" />
        <span class="text-caption">{{ app.tenant_name }}</span>
        <h2>{{ app.name }}</h2>
        <p class="my-3">{{ app.purpose }}</p>
        <v-btn :href="app.url" target="_blank" rel="noopener" color="primary"
          append-icon="mdi-open-in-new" class="app-card__action">アプリを開く</v-btn>
      </v-card>
    </div>
  </section>
</template>
<style scoped>
.app-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, var(--forge-list-card-width)), var(--forge-list-card-width)));
  justify-content: space-between; gap: var(--sp-4); }
.app-card { display: flex; flex-direction: column; }
.app-card p { color: var(--ink-3); }
.app-card__action { align-self: flex-start; margin-top: auto; }
</style>
