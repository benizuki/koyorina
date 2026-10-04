<script setup lang="ts">
import { dayHoursLabel, periodLabels } from '@/profileLabels'
import { onMounted, ref } from 'vue'
import { api } from '@/composables/useApi'
import SourceBrowser from '@/components/SourceBrowser.vue'
import PublicationPanel from '@/components/PublicationPanel.vue'
import ProjectSharing from '@/components/ProjectSharing.vue'
import { usePreview } from '@/composables/usePreview'
import type { GenerationJob, Project } from '@/types'
const props = defineProps<{ project: Project }>()
const categoryLabels: Record<string, string> = { manufacturing: '製造の仕事', indirect: '間接・事務の仕事', other: 'その他' }
const appTypeLabels: Record<string, string> = { records: '入力・管理', visualization: '可視化・分析', both: '入力と可視化の両方' }
const appPatternLabels: Record<string, string> = {
  local_file_visualization: '手元のCSV・Excelを読み込んで可視化',
  data_management: 'データを入力して管理・共有',
  file_import: '手元のデータを処理',
}
const goalLabels: Record<string, string> = {
  production_progress: '累計生産数の進捗', yield: '歩留の推移', production_by_time: '時間帯別の生産性', production_performance: '実績推移',
  defect_pareto: 'パレート図', quality_control_chart: '管理図', equipment_gantt: 'ガントチャート',
  plan_actual_gantt: 'ガントチャート', gantt: 'ガントチャート', daily_records: '日報・点検・帳票', file_visualization: 'グラフ化',
  ledger: '台帳・一覧管理', approval: '申請・承認', inquiry: '問い合わせ・タスク管理', inventory: '在庫・貸出管理',
  schedule: '予定・進捗管理', performance_comparison: '実績の比較', reporting: '集計・レポート', pareto: 'パレート図',
  records: 'データ管理', ai_processing: 'AI処理', other: 'その他',
}
const jobs = ref<GenerationJob[]>([]), loading = ref(false), error = ref(''), showSources = ref(false)
// 管理者は中身を書き換えない。ただし動かしっぱなしは片付けられる必要がある。
// 同時に動かせる数に上限があるので、1つ放置されると他の人が使えなくなる。
const preview = usePreview(props.project.id)
const confirmStop = ref(false)
const labels: Record<string, string> = { stopped: '停止中', starting: '起動中',
                                         running: '実行中', failed: '異常終了' }
const statusLabels: Record<string, string> = { starting: '準備中', generating: '生成中', generated: '生成済み', failed: '失敗' }
async function refresh() {
  loading.value = true; error.value = ''
  try { jobs.value = await api<GenerationJob[]>(`/api/projects/${props.project.id}/jobs`) }
  catch (e) { error.value = e instanceof Error ? e.message : '生成履歴を取得できませんでした。' }
  finally { loading.value = false }
}
onMounted(() => { refresh(); preview.refresh() })
</script>

<template>
  <p class="tip mb-4">作成者：{{ project.owner_name || project.owner_id }}。管理者として閲覧しています。
    仕様の変更と生成は作成者と共同開発者が行います。
    <template v-if="project.can_administer">共同開発者の設定と、プレビューの停止はここでできます。</template></p>
  <v-card class="pa-5 mb-4">
    <h2>仕様（第{{ project.revision }}版）</h2>
    <p class="my-3">{{ project.purpose }}</p>
    <p>{{ project.status === 'approved' ? '仕様承認済み' : '仕様確認中' }}</p>
    <template v-if="project.creation_profile">
      <h3 class="mt-4 mb-2">作るもの</h3>
      <p>{{ appPatternLabels[project.creation_profile.app_pattern] || appTypeLabels[project.creation_profile.app_type] }}／{{ categoryLabels[project.creation_profile.work_category] }}</p>
      <p v-if="project.creation_profile.mode === 'prompt'">依頼文で指定</p>
      <p v-else>{{ project.creation_profile.goals.map(goal => goalLabels[goal] || goal).join('、') }}</p>
      <p v-if="project.creation_profile.production_day_start">1日の始まり：{{ project.creation_profile.production_day_start }}</p>
      <p v-if="project.creation_profile.production_day_hours">1日の稼働時間：{{ dayHoursLabel(project.creation_profile.production_day_hours) }}</p>
      <p v-if="project.creation_profile.default_period">開いたときの期間：{{ periodLabels[project.creation_profile.default_period] }}</p>
      <p v-if="project.creation_profile.sample_data">サンプル：{{ project.creation_profile.sample_data.filename }}（{{ project.creation_profile.sample_data.row_count }}行）</p>
    </template>
    <h3 class="mt-4 mb-2">要件</h3>
    <ul v-if="project.requirements?.length" class="pl-5"><li v-for="(item, index) in project.requirements" :key="index">{{ item }}</li></ul>
    <p v-else>追加の要件はありません。</p>
    <template v-for="table in project.tables" :key="table.name">
      <h3 class="mt-4 mb-2">{{ table.name }}（{{ table.kind === 'master' ? 'マスター' : 'データ' }}）</h3>
      <v-table><thead><tr><th>項目名</th><th>種類</th><th>必須</th></tr></thead>
        <tbody><tr v-for="field in table.fields" :key="field.name"><td>{{ field.name }}</td><td>{{ field.kind }}</td><td>{{ field.required ? '必須' : '任意' }}</td></tr></tbody>
      </v-table>
    </template>
  </v-card>
  <v-card class="pa-5 mb-4">
    <div class="d-flex align-center mb-3"><h2>生成履歴（直近20件）</h2><v-spacer />
      <v-btn variant="text" :loading="loading" @click="refresh">更新</v-btn></div>
    <p class="mb-3">所有者の画面で最後に取得した状態を表示します。</p>
    <v-alert v-if="error" type="error">{{ error }}</v-alert>
    <v-table v-if="jobs.length"><thead><tr><th>依頼日時</th><th>依頼内容</th><th>状態</th><th>モデル</th></tr></thead>
      <tbody><tr v-for="job in jobs" :key="job.id"><td>{{ new Date(job.created_at).toLocaleString('ja-JP') }}</td>
        <td>{{ job.instruction || '仕様から初回生成' }}</td><td>{{ statusLabels[job.status] || job.status }}</td><td>{{ job.model || '—' }}</td></tr></tbody>
    </v-table>
    <p v-else-if="!loading && !error">生成履歴はありません。</p>
  </v-card>
  <v-card v-if="preview.status.value?.enabled" class="pa-5 mb-4">
    <div class="d-flex align-center mb-3">
      <h2>プレビューの実行状態</h2><v-spacer />
      <v-btn variant="text" :loading="preview.loading.value" @click="preview.refresh()">状態を更新</v-btn>
    </div>
    <v-alert v-if="preview.error.value" type="warning" density="compact" class="mb-3">
      {{ preview.error.value }}</v-alert>
    <div class="d-flex align-center" style="gap: var(--sp-3)">
      <span class="chip chip--pill" :class="preview.status.value?.state === 'running' ? 'chip--brand'
        : preview.status.value?.state === 'failed' ? 'chip--danger' : 'chip--warn'">
        {{ labels[preview.status.value?.state ?? 'stopped'] }}</span>
      <!-- 起動は作成者の操作。止めるのは片付けなので、管理者にも渡す。 -->
      <v-btn v-if="project.can_administer && preview.status.value?.state !== 'stopped'"
        variant="outlined" color="error" size="small" @click="confirmStop = true">
        プレビューを止める</v-btn>
    </div>
    <p class="meta mt-3">同時に動かせる数に上限があります。使っていないものは止めてください。</p>
  </v-card>

  <ProjectSharing v-if="project.can_administer" :project="project" class="mb-4" />

  <v-btn variant="outlined" class="mb-4" @click="showSources = !showSources">{{ showSources ? 'ソースコードを閉じる' : 'ソースコードを見る' }}</v-btn>
  <SourceBrowser v-if="showSources" :project-id="project.id" :project-name="project.name" :active="true" :generating="false" />

  <v-card class="pa-5 my-4"><PublicationPanel :project-id="project.id" :jobs="jobs" readonly /></v-card>

  <v-dialog v-model="confirmStop" max-width="520"><v-card class="pa-6">
    <h2>プレビューを止めますか？</h2>
    <p class="my-4">作成者が動かしているプレビューを停止します。
      <strong>展開したコードやデータは消えません。</strong>
      作成者は、あとから同じように起動し直せます。</p>
    <v-card-actions>
      <v-btn variant="outlined" @click="confirmStop = false">キャンセル</v-btn>
      <v-btn color="error" @click="confirmStop = false; preview.stop()">止める</v-btn>
    </v-card-actions>
  </v-card></v-dialog>
</template>
