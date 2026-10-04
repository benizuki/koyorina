<script setup lang="ts">
import { computed, watch } from 'vue'
import type { CreationProfile } from '@/types'

const profile = defineModel<CreationProfile>({ required: true })

const patternOptions = [
  { value: 'local_file_visualization', icon: 'mdi-chart-box-outline',
    title: '手元のCSV・Excelを読み込んで可視化したい',
    text: 'ファイルは保存せず、ブラウザ上で集計・グラフ化します。' },
  { value: 'data_management', icon: 'mdi-database-edit-outline',
    title: 'データを入力して管理・共有したい',
    text: '帳票やマスターをデータベースへ保存します。' },
  { value: 'file_import', icon: 'mdi-database-import-outline',
    title: '手元のデータを処理したい',
    text: 'CSV・Excel・PDFなどを取り込んで保存し、集計やAIでの読み取り・分析を行います。' },
] as const

const allGoals = computed(() => profile.value.work_category === 'manufacturing' ? [
  ['production_performance', '実績推移', '累計生産数、歩留、時間帯別の出来高を表示'],
  ['pareto', 'パレート図', '件数の多い項目と累積比率を表示'],
  ['quality_control_chart', '管理図', '寸法・温度・電流などの推移や管理限界を確認'],
  ['gantt', 'ガントチャート', '設備の稼働や計画・実績を時間軸で表示'],
  ['daily_records', '日報・点検・帳票', '現場の記録を入力して管理'],
] : profile.value.work_category === 'indirect' ? [
  ['ledger', '台帳・一覧管理', '顧客、案件、備品などを登録・検索'],
  ['schedule', '予定・進捗管理', '予定と実績を日付や担当者で確認'],
  ['inquiry', '問い合わせ・タスク管理', '受付、担当、対応状況を管理'],
] : [
  ['file_visualization', 'グラフ化', '取り込んだデータからグラフを作成'],
  ['reporting', '集計・レポート', '蓄積したデータをまとめて確認'],
  ['records', 'データ管理', '取り込んだデータを保存・検索して管理'],
  ['ai_processing', 'AI処理', 'PDFや文章をAI（Gemini）で読み取り・分析して登録'],
])

// 1日の稼働時間と、開いたときの期間。可視化（製造）のときだけ選ぶ。
const hourOptions = [
  { title: '24時間（1日通し）', value: 24 }, { title: '16時間（2直）', value: 16 },
  { title: '12時間', value: 12 }, { title: '8時間（1直）', value: 8 },
]
const periodOptions = [
  { title: '直近1日', value: 'latest_day' }, { title: '直近3日間', value: 'last_3_days' },
  { title: '直近7日間', value: 'last_7_days' }, { title: '直近30日', value: 'last_30_days' },
  { title: '全期間', value: 'all' },
]
// 稼働時間が24時間でないときだけ、どこまでが対象かを示す（例: 06:00〜13:59）。
const windowText = computed(() => {
  const start = profile.value.production_day_start, hours = profile.value.production_day_hours
  if (!start || !hours || hours === 24) return ''
  const [h, m] = start.split(':').map(Number)
  const end = ((h * 60 + m + hours * 60 - 1) % 1440 + 1440) % 1440
  return `${start}〜${String(Math.floor(end / 60)).padStart(2, '0')}:${String(end % 60).padStart(2, '0')}`
})

const workflowGoals = new Set(['daily_records', 'ledger', 'approval', 'inquiry', 'inventory',
  'schedule', 'records', 'other'])
const goals = computed(() => profile.value.app_pattern === 'local_file_visualization'
  ? allGoals.value.filter(goal => !workflowGoals.has(goal[0])) : allGoals.value)

function changePattern(value: CreationProfile['app_pattern']) {
  const appType: CreationProfile['app_type'] = value === 'local_file_visualization' ? 'visualization'
    : value === 'file_import' ? 'both' : 'records'
  const workCategory: CreationProfile['work_category'] = value === 'local_file_visualization' ? 'manufacturing'
    : value === 'data_management' ? 'indirect' : 'other'
  profile.value = {
    ...profile.value,
    app_pattern: value,
    app_type: appType,
    work_category: workCategory,
    goals: [],
    other_goal: '',
    production_day_start: workCategory === 'manufacturing'
      ? profile.value.production_day_start || '06:00' : null,
    production_day_hours: workCategory === 'manufacturing'
      ? profile.value.production_day_hours || 24 : null,
    default_period: workCategory === 'manufacturing'
      ? profile.value.default_period || 'latest_day' : null,
    sample_data: value === 'data_management' ? null : profile.value.sample_data,
    column_mappings: value === 'data_management' ? [] : profile.value.column_mappings,
  }
}

watch(() => profile.value.app_pattern, pattern => {
  const expected: CreationProfile['work_category'] = pattern === 'local_file_visualization' ? 'manufacturing'
    : pattern === 'data_management' ? 'indirect' : 'other'
  if (profile.value.work_category !== expected) changePattern(pattern)
}, { immediate: true })

// 以前に作った仕様には稼働時間・期間が無い。可視化（製造）を開いたら既定値を入れて選べる状態にする。
watch(() => [profile.value.work_category, profile.value.app_pattern], () => {
  if (profile.value.work_category !== 'manufacturing' || profile.value.app_pattern === 'data_management') return
  if (!profile.value.production_day_hours) profile.value.production_day_hours = 24
  if (!profile.value.default_period) profile.value.default_period = 'latest_day'
}, { immediate: true })
</script>

<template>
  <section class="profile-section">
    <div><h2>どんなアプリを作りますか？</h2><p>データを保存するかどうかで、生成する構成を決めます。</p></div>
    <div class="pattern-grid">
      <v-card v-for="option in patternOptions" :key="option.value" class="choice-card pa-4"
        :class="{ chosen: profile.app_pattern === option.value }" tabindex="0" role="radio"
        :aria-checked="profile.app_pattern === option.value"
        @click="changePattern(option.value)" @keydown.enter="changePattern(option.value)">
        <v-icon :icon="option.icon" color="primary" class="mb-2" /><strong>{{ option.title }}</strong>
        <p>{{ option.text }}</p>
      </v-card>
    </div>
    <div><h2>何を作りたいですか？</h2><p>近いものを選んでください。あとでAIと調整できます。</p></div>
    <div class="goal-grid">
      <v-checkbox v-for="goal in goals" :key="goal[0]" v-model="profile.goals" :value="goal[0]"
        hide-details class="goal-choice">
        <template #label><span><strong>{{ goal[1] }}</strong><small>{{ goal[2] }}</small></span></template>
      </v-checkbox>
    </div>
    <p v-if="profile.goals.includes('ai_processing')" class="tip">
      <v-icon icon="mdi-information-outline" />
      <span>AI処理は、テナントに設定された Gemini を使います。まだ設定していない場合は、管理者に
        「システム設定 → テナント → AI設定」の設定を依頼してください。設定が無いと、AI処理以外の部分だけを作ります。</span>
    </p>
    <v-textarea v-if="profile.goals.includes('other')" v-model="profile.other_goal"
      label="その他に作りたいもの" maxlength="500" counter rows="2" />
    <div v-if="profile.work_category === 'manufacturing' && profile.app_pattern !== 'data_management'"
      class="day-grid">
      <v-text-field v-model="profile.production_day_start" type="time" label="1日の始まり"
        hint="例：06:00を選ぶと、翌日の05:59までを同じ生産日として集計します。" persistent-hint />
      <v-select v-model="profile.production_day_hours" :items="hourOptions" label="1日の稼働時間"
        :hint="windowText ? `時間帯別のグラフは ${windowText} を表示します。範囲外は「時間外」にまとめます。`
          : '時間帯別のグラフは、1日の始まりから24時間を表示します。'" persistent-hint />
      <v-select v-model="profile.default_period" :items="periodOptions" label="開いたときの期間"
        hint="読み込んだデータの最新の日から数えます。画面上で変更できます。" persistent-hint />
    </div>
  </section>
</template>

<style scoped>
.profile-section { display: flex; flex-direction: column; gap: var(--sp-5); }
.pattern-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(var(--forge-card-min), 1fr)); gap: var(--sp-3); }
.choice-card { display: flex; flex-direction: column; cursor: pointer; border: 1px solid var(--border-default); }
.choice-card p, .goal-choice small { color: var(--ink-3); }
.chosen { outline: 2px solid var(--border-brand); background: var(--surface-accent); }
.goal-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(var(--forge-card-min), 1fr)); gap: var(--sp-2); }
.goal-choice { border: 1px solid var(--border-default); border-radius: var(--radius-md); padding: var(--sp-2) var(--sp-3); }
.goal-choice span { display: flex; flex-direction: column; gap: var(--sp-1); }
.day-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(var(--forge-card-min), 1fr));
  gap: var(--sp-4); }
</style>
