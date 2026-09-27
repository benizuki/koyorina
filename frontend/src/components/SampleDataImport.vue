<script setup lang="ts">
import { ref } from 'vue'
import ColumnRolePicker from '@/components/ColumnRolePicker.vue'
import { roleOptions } from '@/columnRoles'
import { api } from '@/composables/useApi'
import type { ColumnMapping, ColumnRole, CreationProfile, FieldSpec, SampleDataProfile } from '@/types'

const props = defineProps<{ profile: CreationProfile }>()
const emit = defineEmits<{ 'update:profile': [value: CreationProfile]; 'apply-fields': [value: FieldSpec[]] }>()
const busy = ref(false), error = ref('')
// マス目は一度に1列分だけ開く。全列に並べると、選ぶ場所を見失う。
const openColumn = ref<string>()


const genericRole: Partial<Record<ColumnRole, ColumnRole>> = {
  planned_start: 'start_time', actual_start: 'start_time',
  planned_end: 'end_time', actual_end: 'end_time',
}

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
  error.value = ''
  if (!file) return
  if (file.size > 5 * 1024 * 1024 || !/\.(csv|tsv|xlsx)$/i.test(file.name)) {
    error.value = '5MiB以下のCSV、TSV、または.xlsxファイルを選んでください。'; return
  }
  busy.value = true
  try {
    const sample = await api<SampleDataProfile>('/api/sample-data/analyze', 'POST',
      { name: file.name, content: await encode(file) })
    const mappings = sample.columns.map(column => ({ column: column.name, role: column.suggested_role }))
    emit('update:profile', { ...props.profile, sample_data: sample, column_mappings: mappings })
    const kind = (value: typeof sample.columns[number]['kind']): FieldSpec['kind'] =>
      value === 'number' ? 'number' : value === 'bool' ? 'bool' : ['date', 'datetime'].includes(value) ? 'date' : 'text'
    emit('apply-fields', sample.columns.slice(0, 20).map(column =>
      ({ name: column.name.slice(0, 50), kind: kind(column.kind), required: false })))
  } catch (e) { error.value = e instanceof Error ? e.message : 'ファイルを読み取れませんでした。' }
  finally { busy.value = false }
}

function mappingFor(column: string) {
  const role = props.profile.column_mappings.find(mapping => mapping.column === column)?.role ?? 'none'
  return genericRole[role] ?? role
}
function optionFor(column: string) {
  const role = mappingFor(column)
  return roleOptions.find(option => option.value === role) ?? roleOptions[0]
}
function updateMapping(column: string, role: ColumnRole) {
  const mappings: ColumnMapping[] = props.profile.column_mappings.filter(mapping => mapping.column !== column)
  mappings.push({ column, role })
  emit('update:profile', { ...props.profile, column_mappings: mappings })
  openColumn.value = undefined
}
</script>

<template>
  <section class="sample-import">
    <div><h2>サンプルCSV・Excelを確認する{{ profile.app_pattern === 'file_import' ? '（ある場合）' : '' }}</h2>
      <p>設備ごとに異なる列名と実際の値から候補を出します。元ファイルとデータは保存しません。</p></div>
    <v-file-input label="サンプルファイル" accept=".csv,.tsv,.xlsx" prepend-icon="mdi-file-table-outline"
      :loading="busy" :disabled="busy" show-size @update:model-value="analyze" />
    <v-alert v-if="error" type="error" variant="tonal">{{ error }}</v-alert>
    <template v-if="profile.sample_data">
      <v-alert type="success" variant="tonal">{{ profile.sample_data.filename }}<span v-if="profile.sample_data.sheet">・{{ profile.sample_data.sheet }}</span>：{{ profile.sample_data.row_count }}行、{{ profile.sample_data.columns.length }}列を確認しました。</v-alert>
      <v-alert v-for="warning in profile.sample_data.warnings" :key="warning" type="info" variant="tonal">{{ warning }}</v-alert>
      <p>列の役割を確認してください。表記が違っていても、ここで正しい意味に直せます。</p>
      <div class="mapping-list">
        <div v-for="column in profile.sample_data.columns" :key="column.name" class="mapping-row"
          :class="{ 'is-open': openColumn === column.name }">
          <div class="mapping-head">
            <div class="column-info"><strong>{{ column.name }}</strong>
              <small>例：{{ column.samples.slice(0, 3).join('、') || '値なし' }}</small></div>
            <button type="button" class="current-role" :aria-expanded="openColumn === column.name"
              :class="{ 'is-unused': optionFor(column.name).value === 'none' }"
              @click="openColumn = openColumn === column.name ? undefined : column.name">
              <v-icon :icon="optionFor(column.name).icon" size="small" />
              <span>{{ optionFor(column.name).title }}</span>
              <v-icon :icon="openColumn === column.name ? 'mdi-chevron-up' : 'mdi-chevron-down'" size="small" class="ml-auto" />
            </button>
          </div>
          <ColumnRolePicker v-if="openColumn === column.name" :model-value="mappingFor(column.name)"
            @update:model-value="value => updateMapping(column.name, value)" />
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.sample-import { display: flex; flex-direction: column; gap: var(--sp-4); }
.mapping-list { display: flex; flex-direction: column; gap: var(--sp-2); }
.mapping-row { display: flex; flex-direction: column; gap: var(--sp-3); padding: var(--sp-3); border: 1px solid var(--border-default); border-radius: var(--radius-md); }
.mapping-row.is-open { border-color: var(--border-brand); }
.mapping-head { display: grid; grid-template-columns: minmax(0, 1fr) minmax(var(--forge-card-min), 1fr); gap: var(--sp-3); align-items: center; }
.column-info { display: flex; flex-direction: column; min-width: 0; }
.column-info small { color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.current-role { display: flex; align-items: center; gap: var(--sp-2); min-height: var(--control-lg); padding: var(--sp-2) var(--sp-3);
  color: var(--ink-1); background: var(--surface-accent); border: 1px solid var(--border-brand);
  border-radius: var(--radius-md); font-weight: var(--fw-medium); text-align: left; cursor: pointer; }
.current-role.is-unused { color: var(--ink-3); background: var(--surface-subtle); border-color: var(--border-default); }
.current-role:focus-visible { outline: 2px solid var(--border-brand); outline-offset: 2px; }
@media (max-width: 600px) { .mapping-head { grid-template-columns: 1fr; } }
</style>
