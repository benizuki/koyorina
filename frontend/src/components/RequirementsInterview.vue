<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRequirementsInterview } from '@/composables/useRequirementsInterview'
import type { Project, TableSpec } from '@/types'

const props = defineProps<{ project: Project; canUseCodex: boolean; geminiAvailable: boolean }>()
const emit = defineEmits<{ applied: [project: Project] }>()
const provider = ref<'codex' | 'gemini'>(props.canUseCodex ? 'codex' : 'gemini')
const interview = useRequirementsInterview(props.project.id, provider)
const appliedOnce = ref(false)
const choices = reactive<Record<string, string>>({})
const other = reactive<Record<string, string>>({})
const questions = computed(() => interview.state.value.questions ||
  (interview.state.value.question ? [interview.state.value.question] : []))
const canAnswer = computed(() => questions.value.length > 0 && questions.value.every(q =>
  choices[q.id] && (choices[q.id] !== '__other__' || other[q.id]?.trim())))
async function submit() {
  const answers = Object.fromEntries(questions.value.map(q => [q.id,
    [choices[q.id] === '__other__' ? other[q.id].trim() : choices[q.id]]]))
  await interview.answer(answers)
}
// 反映してよいかは、何が変わるのかが見えないと決められない。いまの仕様と突き合わせる。
const result = computed(() => interview.state.value.result ?? null)
const names = (table?: TableSpec) => (table?.fields ?? []).map(field => field.name)
const label = (table: TableSpec) => table.kind === 'master' ? 'マスター' : '帳票'
const changes = computed(() => {
  const after = result.value
  if (!after) return null
  const before = props.project
  const was = before.requirements ?? [], now = after.requirements ?? []
  const requirements = {
    added: now.filter(item => !was.includes(item)),
    removed: was.filter(item => !now.includes(item)),
  }
  const previous = new Map(before.tables.map(table => [table.name, table]))
  const current = new Map(after.tables.map(table => [table.name, table]))
  const tables: { key: string; kind: 'added' | 'removed' | 'changed'; text: string }[] = []
  for (const [name, table] of current) {
    const older = previous.get(name)
    if (!older) {
      tables.push({ key: name, kind: 'added',
        text: `${name}（${label(table)}）を追加：${names(table).join('、') || '項目なし'}` })
      continue
    }
    const added = names(table).filter(field => !names(older).includes(field))
    const removed = names(older).filter(field => !names(table).includes(field))
    if (added.length || removed.length) {
      const parts = [added.length ? `＋${added.join('、')}` : '', removed.length ? `－${removed.join('、')}` : '']
      tables.push({ key: name, kind: 'changed', text: `${name}：${parts.filter(Boolean).join('　')}` })
    }
  }
  for (const name of previous.keys()) {
    if (!current.has(name)) tables.push({ key: name, kind: 'removed', text: `${name} を削除` })
  }
  const purpose = before.purpose === after.purpose ? '' : after.purpose
  const profileChanged = JSON.stringify(before.creation_profile ?? null) !== JSON.stringify(after.creation_profile ?? null)
  return { purpose, requirements, tables, profileChanged,
    any: Boolean(purpose || requirements.added.length || requirements.removed.length || tables.length || profileChanged) }
})

async function apply() {
  const project = await interview.apply(props.project.revision)
  if (project) { appliedOnce.value = true; emit('applied', project) }
}
onMounted(interview.refresh)
watch(provider, async () => {
  appliedOnce.value = false
  Object.keys(choices).forEach(key => delete choices[key])
  Object.keys(other).forEach(key => delete other[key])
  interview.state.value = { status: 'idle', provider: provider.value }
  await interview.refresh()
})
</script>

<template>
  <v-card variant="tonal" class="pa-5 my-5 interview">
    <div class="interview-heading">
      <div><h3>AIで要件をもう少し整理</h3>
        <p class="mt-1">最初のアプリを作るために欠かせないことだけを、最大3問で確認します。
        細かな部分は作った画面を見ながら後で直せます。</p></div>
      <!-- 主操作は塗る。tonalなカードの上で outlined にすると、押せないものと
           見分けが付かない（既定の variant は flat なので、上書きしない）。 -->
      <v-btn v-if="['idle', 'failed', 'cancelled'].includes(interview.state.value.status)" color="primary"
        prepend-icon="mdi-comment-question-outline"
        :loading="interview.loading.value" @click="interview.start">ヒアリングを始める</v-btn>
    </div>
    <div v-if="props.canUseCodex && props.geminiAvailable" class="provider mt-4">
      <span class="option-note">確認に使うAI</span>
      <!-- 選択中を塗り分ける。outlined 同士だと、どちらが選ばれているか分からない。 -->
      <v-btn-toggle v-model="provider" mandatory color="primary" variant="flat" divided
        density="comfortable"
        :disabled="!['idle', 'failed', 'cancelled', 'completed'].includes(interview.state.value.status)">
        <v-btn value="codex">Codex</v-btn><v-btn value="gemini">Gemini</v-btn>
      </v-btn-toggle>
    </div>
    <p v-else class="option-note mt-3">{{ provider === 'gemini' ? 'Geminiで確認します。' : 'Codexで確認します。' }}</p>
    <v-alert v-if="interview.error.value || interview.state.value.error" type="warning" variant="tonal" class="mt-4">
      {{ interview.error.value || interview.state.value.error }}
    </v-alert>
    <div v-if="['starting', 'thinking'].includes(interview.state.value.status)" class="mt-4">
      <v-progress-linear indeterminate color="primary" /><p class="mt-2">次の確認内容を整理しています。</p>
    </div>
    <div v-if="interview.state.value.status === 'waiting'" class="questions mt-5">
      <p v-if="interview.state.value.round" class="eyebrow">
        {{ interview.state.value.round }}回目
        {{ interview.state.value.rounds ? `／ 最大${interview.state.value.rounds}回` : '' }}
      </p>
      <section v-for="question in questions" :key="question.id">
        <p class="eyebrow">{{ question.header }}</p><h3>{{ question.question }}</h3>
        <v-radio-group v-model="choices[question.id]" class="mt-2">
          <v-radio v-for="option in question.options" :key="option.label" :value="option.label">
            <template #label><div><strong>{{ option.label }}</strong><p class="option-note">{{ option.description }}</p></div></template>
          </v-radio>
          <v-radio v-if="question.is_other !== false" label="自分で入力" value="__other__" />
        </v-radio-group>
        <v-text-field v-if="choices[question.id] === '__other__'" v-model="other[question.id]"
          label="回答を入力" maxlength="1000" />
      </section>
      <div class="actions"><v-btn variant="text" @click="interview.cancel">中止</v-btn>
        <v-btn color="primary" :disabled="!canAnswer" :loading="interview.loading.value" @click="submit">回答して次へ</v-btn></div>
      <p class="option-note">質問が少ない場合もあります。回答後はすぐ仕様案を確認できます。</p>
    </div>
    <div v-if="interview.state.value.status === 'completed' && !appliedOnce" class="mt-4">
      <v-alert type="success" variant="tonal">ヒアリングがまとまりました。反映すると下のとおり変わります。</v-alert>
      <div v-if="changes" class="summary mt-4">
        <p v-if="!changes.any" class="tip tip--compact">
          <v-icon icon="mdi-information-outline" />
          <span>いまの仕様から変わるところはありません。</span>
        </p>
        <template v-else>
          <section v-if="changes.purpose">
            <p class="eyebrow">目的</p>
            <p>{{ changes.purpose }}</p>
          </section>
          <section v-if="changes.requirements.added.length || changes.requirements.removed.length">
            <p class="eyebrow">確認した要件</p>
            <ul class="diff">
              <li v-for="item in changes.requirements.added" :key="`add-${item}`" class="diff--added">
                ＋ {{ item }}</li>
              <li v-for="item in changes.requirements.removed" :key="`del-${item}`" class="diff--removed">
                － {{ item }}</li>
            </ul>
          </section>
          <section v-if="changes.tables.length">
            <p class="eyebrow">帳票・マスター</p>
            <ul class="diff">
              <li v-for="entry in changes.tables" :key="entry.key" :class="`diff--${entry.kind}`">
                {{ entry.text }}</li>
            </ul>
          </section>
          <section v-if="changes.profileChanged">
            <p class="eyebrow">作るもの・データの割り当て</p>
            <p>可視化の目的、集計時刻、または列の割り当てが更新されます。</p>
          </section>
        </template>
      </div>
      <v-btn color="primary" class="mt-4" :loading="interview.loading.value" @click="apply">
        この内容で仕様へ反映</v-btn>
    </div>
    <v-alert v-if="appliedOnce" type="success" variant="tonal" class="mt-4">整理した要件を仕様へ反映しました。内容を確認して仕様を確定できます。</v-alert>
  </v-card>
</template>

<style scoped>
.interview { flex: 0 0 auto; min-height: fit-content; overflow: visible; font-size: var(--fs-sm); }
.interview h3 { font-size: var(--fs-md); }
.interview :deep(.v-label), .interview :deep(.v-btn), .interview :deep(.v-alert) { font-size: var(--fs-sm); }
.interview-heading, .actions { display: flex; justify-content: space-between; gap: var(--sp-4); align-items: center; flex-wrap: wrap; }
.interview-heading > div { flex: 1 1 300px; }
.interview-heading .v-btn { flex: 0 0 auto; }
.questions { display: flex; flex-direction: column; gap: var(--sp-5); }
.option-note { color: var(--ink-3); font-size: var(--fs-xs); }
.provider { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }
.provider :deep(.v-btn-toggle) { gap: var(--sp-2); }
/* 選ばれていない側も読める濃さにする。既定のままだと、選択中との差が
   「薄いか、もっと薄いか」になり、どちらが有効なのか分からない。 */
.provider :deep(.v-btn--variant-flat:not(.v-btn--active)) {
  background: var(--surface-subtle); color: var(--ink-2);
}
.eyebrow { color: var(--text-brand); font-size: var(--fs-xs); font-weight: var(--fw-bold); }
.summary { display: flex; flex-direction: column; gap: var(--sp-4); }
/* 差分は記号と色の両方で示す。色だけだと、増えたのか減ったのか読み取れない人がいる。 */
.diff { list-style: none; padding: 0; margin: var(--sp-1) 0 0; display: flex; flex-direction: column; gap: var(--sp-1); }
.diff li { padding: var(--sp-1) var(--sp-2); border-radius: var(--radius-sm); }
.diff--added   { color: var(--text-brand);  background: var(--surface-accent); }
.diff--removed { color: var(--text-danger); background: var(--surface-danger); }
.diff--changed { color: var(--ink-2);       background: var(--surface-muted); }
</style>
