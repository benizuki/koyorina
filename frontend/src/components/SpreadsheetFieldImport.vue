<script setup lang="ts">
import { ref } from 'vue'
import { api } from '@/composables/useApi'
import type { FieldSpec, SampleDataProfile } from '@/types'

const emit = defineEmits<{ apply: [value: FieldSpec[]] }>()
const busy = ref(false), error = ref(''), result = ref<SampleDataProfile | null>(null)

async function encode(file: File) {
  return await new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error(`${file.name}を読み取れませんでした。`))
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1] ?? '')
    reader.readAsDataURL(file)
  })
}

async function analyze(files: File | File[] | null) {
  const file = Array.isArray(files) ? files[0] : files
  error.value = ''; result.value = null
  if (!file) return
  if (file.size > 5 * 1024 * 1024 || !/\.(csv|tsv|xlsx)$/i.test(file.name)) {
    error.value = '5MiB以下のCSV、TSV、または.xlsxファイルを選んでください。'; return
  }
  busy.value = true
  try {
    const sample = await api<SampleDataProfile>('/api/sample-data/analyze', 'POST',
      { name: file.name, content: await encode(file) })
    const kind = (value: typeof sample.columns[number]['kind']): FieldSpec['kind'] =>
      value === 'number' ? 'number' : value === 'bool' ? 'bool'
        : ['date', 'datetime'].includes(value) ? 'date' : 'text'
    emit('apply', sample.columns.slice(0, 20).map(column =>
      ({ name: column.name.slice(0, 50), kind: kind(column.kind), required: false })))
    result.value = sample
  } catch (e) { error.value = e instanceof Error ? e.message : 'ファイルを読み取れませんでした。' }
  finally { busy.value = false }
}
</script>

<template>
  <section class="sheet-import">
    <div><h3>CSV・Excelから項目を読み取る</h3>
      <p>既存の台帳や帳票の列名を、入力項目の候補として取り込みます。元ファイルと値は保存しません。</p></div>
    <v-file-input label="帳票・台帳ファイル" accept=".csv,.tsv,.xlsx"
      prepend-icon="mdi-file-table-outline" :loading="busy" :disabled="busy" show-size
      @update:model-value="analyze" />
    <v-alert v-if="error" type="error" variant="tonal">{{ error }}</v-alert>
    <v-alert v-if="result" type="success" variant="tonal">
      {{ result.columns.length }}件の入力項目を取り込みました。下の一覧で名前や種類を修正できます。
    </v-alert>
  </section>
</template>

<style scoped>
.sheet-import { display: flex; flex-direction: column; gap: var(--sp-3); padding: var(--sp-4); border: 1px solid var(--border-default); border-radius: var(--radius-md); }
.sheet-import p { color: var(--ink-3); }
</style>
