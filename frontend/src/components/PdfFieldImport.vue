<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ExtractedFields, FieldSpec } from '@/types'
import { usePdfFields } from '@/composables/usePdfFields'
defineProps<{ enabled: boolean }>()
const emit = defineEmits<{ apply: [fields: FieldSpec[]] }>()
const { busy, error, extract } = usePdfFields()
const file = ref<File | File[] | null>(null), consent = ref(false), reviewed = ref(false)
const candidates = ref<ExtractedFields>()
const selectedFiles = computed(() => !file.value ? [] : Array.isArray(file.value) ? file.value : [file.value])
const valid = computed(() => candidates.value && candidates.value.fields.length > 0 &&
  candidates.value.fields.every(f => f.name.trim()) &&
  new Set(candidates.value.fields.map(f => f.name.trim().toLowerCase())).size === candidates.value.fields.length)
const kinds = [{ title: '短い文字', value: 'text' }, { title: '長い文章', value: 'longtext' },
  { title: '数値', value: 'number' }, { title: '日付', value: 'date' }, { title: 'はい／いいえ', value: 'bool' }]
async function run() {
  if (!consent.value || !selectedFiles.value.length) return
  reviewed.value = false
  candidates.value = await extract(selectedFiles.value)
}
function apply() {
  if (!valid.value || !reviewed.value || !candidates.value) return
  emit('apply', candidates.value.fields.map(f => ({ ...f, name: f.name.trim() })))
  candidates.value = undefined; file.value = null; consent.value = false
}
</script>

<template>
  <v-card variant="outlined" class="pa-5">
    <h2>帳票PDFから項目を読み取る</h2>
    <p class="my-3">複数の領収書や申請書をまとめて読み、重複を整理した入力項目の候補を作れます。記入済みの値ではなく、項目名を取り込みます。</p>
    <p v-if="!enabled" class="tip tip--warn">Gemini APIの接続設定待ちです。下の入力欄から手入力できます。</p>
    <v-file-input v-model="file" multiple chips clearable accept="application/pdf,.pdf"
      label="帳票PDF（最大5件・各5MiB・合計15MiB以下）" :disabled="!enabled || busy" class="my-4" />
    <v-checkbox v-model="consent" :disabled="!enabled || busy" label="選択したPDFを設定済みのGemini APIへ送信して項目を抽出することを確認しました" />
    <v-btn variant="outlined" :loading="busy" :disabled="!enabled || !selectedFiles.length || !consent" @click="run">まとめて項目候補を抽出</v-btn>
    <v-alert v-if="error" type="error" class="mt-3">{{ error }}</v-alert>
    <v-dialog :model-value="!!candidates" max-width="900" @update:model-value="v => { if (!v) candidates = undefined }">
      <v-card class="pa-6">
        <h2>読み取った項目を確認してください</h2>
        <p class="my-4">AIの推測を含みます。項目名・型・必須を確認してください。「反映する」で現在の入力項目を置き換えます。保存済みの仕様はまだ変更しません。</p>
        <p v-for="(warning, i) in candidates?.warnings" :key="i" class="tip tip--warn mb-3">{{ warning }}</p>
        <div v-for="(field, i) in candidates?.fields" :key="i" class="d-flex flex-wrap ga-3 mb-4">
          <v-text-field v-model="field.name" :label="`候補${i + 1}の項目名`" maxlength="50" />
          <v-select v-model="field.kind" :items="kinds" label="入力内容" />
          <v-checkbox v-model="field.required" label="必須" />
          <v-btn icon="mdi-minus-circle-outline" variant="text" :aria-label="`候補${i + 1}を外す`" @click="candidates?.fields.splice(i, 1)" />
        </div>
        <v-checkbox v-model="reviewed" label="候補を確認し、現在の入力項目を置き換えることを確認しました" />
        <v-card-actions><v-btn @click="candidates = undefined">キャンセル</v-btn><v-btn color="primary" :disabled="!reviewed || !valid" @click="apply">反映する</v-btn></v-card-actions>
      </v-card>
    </v-dialog>
  </v-card>
</template>
