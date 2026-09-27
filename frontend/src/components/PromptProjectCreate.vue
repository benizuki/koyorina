<script setup lang="ts">
import { computed, ref } from 'vue'
import type { CreationProfile, Project, ProjectInput } from '@/types'

// 帳票やサンプルを選ばず、依頼文だけでアプリを作る入口。
// 構成（ブラウザ完結かデータベースありか）は依頼文から推測させず、ここで選ばせる。
const props = defineProps<{ project?: Project; loading: boolean; tenantChoices: { title: string; value: string }[] }>()
const emit = defineEmits<{ submit: [input: ProjectInput]; cancel: [] }>()
const tenantId = defineModel<string | undefined>('tenantId')

const patternOptions = [
  { value: 'local_file_visualization', icon: 'mdi-chart-box-outline', title: '手元のファイルを可視化',
    text: 'CSV・Excelをブラウザで読み込みます。データは保存しません。' },
  { value: 'data_management', icon: 'mdi-database-edit-outline', title: 'データを入力して管理',
    text: '帳票やマスターをデータベースへ保存します。' },
  { value: 'file_import', icon: 'mdi-database-import-outline', title: '手元のデータを処理',
    text: 'CSV・Excel・PDFなどを取り込んで保存し、集計やAIで読み取り・分析します。' },
] as const

// バックエンドも app_pattern から同じ値へ揃える。ここでは型を満たすために送る。
const appTypes: Record<CreationProfile['app_pattern'], CreationProfile['app_type']> = {
  local_file_visualization: 'visualization', data_management: 'records', file_import: 'both' }
const workCategories: Record<CreationProfile['app_pattern'], CreationProfile['work_category']> = {
  local_file_visualization: 'manufacturing', data_management: 'indirect', file_import: 'other' }

const name = ref(props.project?.name ?? '')
const pattern = ref<CreationProfile['app_pattern']>(props.project?.creation_profile?.app_pattern ?? 'data_management')
const prompt = ref(props.project?.generation_prompt ?? '')
const cleanPrompt = computed(() => prompt.value.trim())
const valid = computed(() => !!name.value.trim() && cleanPrompt.value.length >= 5)

function submit() {
  if (!valid.value) return
  const current = props.project
  const profile: CreationProfile = { app_pattern: pattern.value, mode: 'prompt',
    work_category: workCategories[pattern.value], app_type: appTypes[pattern.value], goals: [],
    other_goal: '', production_day_start: null, production_day_hours: null, default_period: null,
    sample_data: null, column_mappings: [] }
  emit('submit', { name: name.value.trim(), purpose: '', audience: current?.audience ?? 'team',
    fields: [], tables: [], requirements: current?.requirements ?? [],
    generation_prompt: cleanPrompt.value, creation_profile: profile })
}
</script>

<template>
  <v-card class="pa-6 mt-4">
    <div class="form-stack">
      <div>
        <h1>{{ project ? '依頼文を修正します' : 'プロンプトから作ります' }}</h1>
        <p class="mt-2 lead">作りたいものを自由に書いてください。書いた文章をそのままAIへの依頼文にします。</p>
      </div>
      <v-text-field v-model="name" label="アプリの名前" placeholder="例：設備稼働ガント" maxlength="80" counter />
      <div>
        <h2>どんな構成にしますか？</h2>
        <p class="lead">データを保存するかどうかで、生成する構成が変わります。</p>
      </div>
      <div class="pattern-grid" role="radiogroup" aria-label="アプリの構成">
        <v-card v-for="option in patternOptions" :key="option.value" class="choice-card pa-4"
          :class="{ chosen: pattern === option.value }" tabindex="0" role="radio"
          :aria-checked="pattern === option.value"
          @click="pattern = option.value" @keydown.enter="pattern = option.value">
          <v-icon :icon="option.icon" color="primary" class="mb-2" /><strong>{{ option.title }}</strong>
          <p>{{ option.text }}</p>
        </v-card>
      </div>
      <v-textarea v-model="prompt" label="AIへの依頼文" rows="10" maxlength="10000" counter
        placeholder="例：設備ごとの稼働・停止・故障の時間帯をガントチャートで表示したい。&#10;TSVファイル（設備、状態、開始日時、終了日時）を読み込む。&#10;故障は赤、段取りは黄色で色分けする。" />
      <p class="tip">使う人、扱うデータの項目、見たいグラフや画面を具体的に書くほど、意図に近いアプリになります。
        作成後も仕様画面で依頼文を直せます。ここではまだアプリの生成を開始しません。</p>
      <v-select v-if="!project && tenantChoices.length > 1" v-model="tenantId" :items="tenantChoices"
        label="どのテナントのアプリですか？" hint="利用量をこの単位で数えます。あとから移せます。" persistent-hint />
      <div class="actions">
        <v-btn variant="text" @click="emit('cancel')">キャンセル</v-btn>
        <v-btn color="primary" :loading="loading" :disabled="!valid" @click="submit">
          {{ project ? '保存して仕様画面へ' : '作成して仕様画面へ' }}</v-btn>
      </div>
    </div>
  </v-card>
</template>

<style scoped>
.form-stack { display: flex; flex-direction: column; gap: var(--sp-5); }
.lead { color: var(--ink-3); }
.pattern-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(var(--forge-card-min), 1fr)); gap: var(--sp-3); }
.choice-card { display: flex; flex-direction: column; cursor: pointer; border: 1px solid var(--border-default); }
.choice-card p { color: var(--ink-3); }
.chosen { outline: 2px solid var(--border-brand); background: var(--surface-accent); }
.actions { display: flex; justify-content: flex-end; gap: var(--sp-3); }
</style>
