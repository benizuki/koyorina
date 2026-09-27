<script setup lang="ts">
import { dayHoursLabel, periodLabels } from '@/profileLabels'
import { computed, onMounted, ref, watch } from 'vue'
import ChatPanel from './ChatPanel.vue'
import PreviewControls from './PreviewControls.vue'
import PreviewFrame from './PreviewFrame.vue'
import SourceBrowser from './SourceBrowser.vue'
import { useCodex } from '@/composables/useCodex'
import ChangeHistory from './ChangeHistory.vue'
import { useDeployments } from '@/composables/useDeployments'
import { useDevelopmentSession } from '@/composables/useDevelopmentSession'
import { useDoneNotice } from '@/composables/useDoneNotice'
import { useGeneration } from '@/composables/useGeneration'
import { usePreview } from '@/composables/usePreview'
import { stored } from '@/composables/useStored'
import type { Project } from '@/types'
const props = defineProps<{ project: Project; enabled: boolean; previewEnabled: boolean
  shellEnabled?: boolean; localCodexEnabled?: boolean }>()
const emit = defineEmits<{ connect: []; editSpec: []; saveRequirements: [requirements: string[]]; savePrompt: [prompt: string] }>()
const { jobs, loading, error, refresh, generate, sendInstruction, cancel, revalidate, revalidation,
        downloadLocalPackage, uploadLocalArtifact } = useGeneration(props.project.id)
const { status, refresh: refreshAccount } = useCodex()
// 未確定のうちは仕様から見せる。確定の操作は仕様タブにしかないので、
// プレビューから始めると「どこで確定するのか」が分からない。
const side = ref(props.project.status === 'draft' ? 'spec' : 'preview')
const specSection = ref<'profile' | 'records' | 'masters' | 'requirements'>(
  props.project.creation_profile ? 'profile' : 'records')
const specTable = ref(0)
const specTables = computed(() => props.project.tables.filter(table =>
  table.kind === (specSection.value === 'masters' ? 'master' : 'record')))
const requirementsDraft = ref<string[]>([])
const promptDraft = ref('')
const promptEditing = ref(false)
watch(() => props.project, project => {
  requirementsDraft.value = [...(project.requirements ?? [])]
  promptDraft.value = project.generation_prompt
  promptEditing.value = false
}, { immediate: true })
watch(specSection, () => { specTable.value = 0 })
const cleanRequirements = computed(() => requirementsDraft.value.map(item => item.trim()).filter(Boolean))
const requirementsValid = computed(() => cleanRequirements.value.every(item => item.length <= 500)
  && new Set(cleanRequirements.value.map(item => item.toLowerCase())).size === cleanRequirements.value.length)
const requirementsChanged = computed(() => JSON.stringify(cleanRequirements.value)
  !== JSON.stringify(props.project.requirements ?? []))
const cleanPrompt = computed(() => promptDraft.value.trim())
const promptValid = computed(() => cleanPrompt.value.length >= 5 && cleanPrompt.value.length <= 10000)
const promptChanged = computed(() => cleanPrompt.value !== props.project.generation_prompt.trim())
function startPromptEdit() {
  promptDraft.value = props.project.generation_prompt
  promptEditing.value = true
}
function cancelPromptEdit() {
  promptDraft.value = props.project.generation_prompt
  promptEditing.value = false
}
// 状態は1か所で持つ。タブを分けても同じ実行環境を見るため。
const preview = usePreview(props.project.id)
const localFile = ref<File>(), localNotice = ref('')
const { deployments: history, error: historyError, refresh: loadHistory } = useDeployments(props.project.id)
// 変更履歴はファイルタブへ。いまのファイルと差分を同じ場所で見る。
const fileView = ref<'current' | 'changes'>('current')
const changeList = ref<InstanceType<typeof ChangeHistory>>()
watch(side, value => { if (value === 'history') loadHistory() })
const active = computed(() => jobs.value.some(j => ['starting', 'generating'].includes(j.status)))
// 左右の比率。掴んで動かせるようにし、選んだ幅は覚えておく。
const panes = ref<HTMLElement>()
const ratio = stored('pane-ratio', 40)
const dragging = ref(false)
function grab(event: PointerEvent) {
  dragging.value = true
  ;(event.target as HTMLElement).setPointerCapture(event.pointerId)
}
function move(event: PointerEvent) {
  if (!dragging.value || !panes.value) return
  const box = panes.value.getBoundingClientRect()
  // 端まで詰めると片側が読めなくなる。20〜75%に収める。
  ratio.value = Math.min(75, Math.max(20, Math.round(((event.clientX - box.left) / box.width) * 100)))
}
function release(event: PointerEvent) {
  dragging.value = false
  ;(event.target as HTMLElement).releasePointerCapture(event.pointerId)
}
// キーボードでも動かせるようにする。掴む操作を強いない。
function nudge(step: number) { ratio.value = Math.min(75, Math.max(20, ratio.value + step)) }
const kinds: Record<string, string> = { text: '短い文字', longtext: '長い文章', number: '数値',
  date: '日付', bool: 'はい／いいえ' }
const appTypeLabels: Record<string, string> = { records: '入力・管理', visualization: '可視化・分析', both: '入力と可視化の両方' }
const appPatternLabels: Record<string, string> = {
  local_file_visualization: '手元のCSV・Excelを読み込んで可視化',
  data_management: 'データを入力して管理・共有',
  file_import: '手元のデータを処理',
}
const goalLabels: Record<string, string> = {
  production_progress: '累計生産数の進捗', yield: '歩留の推移', production_by_time: '時間帯別の生産性', production_performance: '実績推移',
  defect_pareto: 'パレート図', quality_control_chart: '管理図',
  equipment_gantt: 'ガントチャート', plan_actual_gantt: 'ガントチャート', gantt: 'ガントチャート',
  daily_records: '日報・点検・帳票', file_visualization: 'グラフ化', ledger: '台帳・一覧管理',
  approval: '申請・承認', inquiry: '問い合わせ・タスク管理', inventory: '在庫・貸出管理', schedule: '予定・進捗管理',
  performance_comparison: '実績の比較', reporting: '集計・レポート', pareto: 'パレート図', records: 'データ管理', ai_processing: 'AI処理', other: 'その他',
}
type Choice = { model: string; effort: string; provider: string; chat_id: string }
async function run(choice: Choice) { await generate(choice) }
async function instruct(text: string, choice: Choice) { await sendInstruction(text, choice) }
// 接続状態は生成が終わってから取り直す。実行中に聞いても実行環境は直前の値を返すだけで、
// 依頼直後に聞くと、作業用の接続を用意している最中に割り込むことになる。
watch(active, (now, before) => {
  if (!before || now) return
  refreshAccount()
  if (side.value === 'files' && fileView.value === 'changes') changeList.value?.refresh()
})
// 生成結果の履歴記録は、ジョブ完了通知の直後に作られる。状態だけ監視すると
// 同じ状態の再読込を取りこぼすため、ジョブIDと状態の組が変わった時点で再取得する。
watch(() => jobs.value.map(job => `${job.id}:${job.status}`).join('|'), (now, before) => {
  if (now !== before && side.value === 'files' && fileView.value === 'changes') {
    changeList.value?.refresh()
  }
})
// 終わったことを知らせる。待っている間は別の作業をしているため、画面内の表示だけでは届かない。
const { notice, shown: noticeShown } = useDoneNotice(jobs, active, preview.status)
async function downloadPackage() {
  localNotice.value = ''
  if (await downloadLocalPackage()) localNotice.value = 'ダウンロードを開始しました。'
}
async function uploadLocal() {
  if (!localFile.value) return
  if (await uploadLocalArtifact(localFile.value)) localFile.value = undefined
}
// 開発の席。先に入った人だけが編集でき、後から入った人は閲覧のみになる。
const { session, takeOver, beat } = useDevelopmentSession(props.project.id)
const readOnly = computed(() => session.value ? !session.value.editable : false)
onMounted(async () => { await Promise.all([refresh(), refreshAccount(), beat()]) })
</script>

<template>
  <div class="workspace">
    <v-alert v-if="error" type="error" class="mb-4">{{ error }}</v-alert>
    <!-- 誰が開発中かを先に見せる。作業してから弾かれるのを避ける。 -->
    <v-alert v-if="readOnly" type="warning" variant="tonal" class="mb-4">
      <div class="holder">
        <span><strong>{{ session?.holder_name }}</strong> さんがこのアプリを開発中です。
          いまは閲覧のみです。仕様・生成・プレビューの操作はできません。</span>
        <v-btn v-if="session?.can_take_over" variant="outlined" @click="takeOver">開発を引き取る</v-btn>
      </div>
    </v-alert>
    <!-- 自動では閉じない。席を外している間に消えると、知らせた意味がなくなる。
         色は styles/base.css の .notice。Vuetifyの既定の塗りつぶしは使わない。 -->
    <v-snackbar v-model="noticeShown" :timeout="-1" location="bottom"
      :class="notice?.type === 'error' ? 'notice notice--error' : 'notice'">
      <v-icon :icon="notice?.type === 'error' ? 'mdi-alert-circle-outline' : 'mdi-check-circle-outline'" />
      <span>{{ notice?.text }}</span>
      <template #actions>
        <v-btn variant="text" @click="noticeShown = false">閉じる</v-btn>
      </template>
    </v-snackbar>
    <div ref="panes" class="panes" :style="{ gridTemplateColumns: `${ratio}% var(--sp-2) 1fr` }">
      <v-card class="pa-5 pane">
        <h2 class="mb-4">開発</h2>
        <div class="requirements-shell"><slot name="requirements" /></div>
        <ChatPanel :project="project" :jobs="jobs" :loading="loading" :enabled="enabled && !readOnly"
          :status="status" :active="active"
          @generate="run" @instruct="instruct" @cancel="cancel" @revalidate="revalidate"
          @connect="emit('connect')" />
      </v-card>
      <div class="grip" role="separator" tabindex="0" aria-label="左右の幅を調整"
        :aria-valuenow="ratio" aria-valuemin="20" aria-valuemax="75"
        @pointerdown="grab" @pointermove="move" @pointerup="release" @pointercancel="release"
        @keydown.left.prevent="nudge(-5)" @keydown.right.prevent="nudge(5)" />
      <v-card class="pane pane--side">
        <v-tabs v-model="side" density="comfortable">
          <!-- 見る順に並べる。動かして確かめ、仕様と履歴をたどり、必要ならファイルを開く。 -->
          <v-tab value="preview">プレビュー</v-tab>
          <v-tab value="runtime">実行管理</v-tab>
          <v-tab value="spec">仕様</v-tab>
          <v-tab value="history">開発履歴</v-tab>
          <v-tab value="files">ファイル</v-tab>
          <v-tab value="sharing">共有・テナント</v-tab>
        </v-tabs>
        <div class="side-body pa-5"
          :class="{ 'side-body--profile': side === 'spec' && specSection === 'profile',
            'side-body--runtime': side === 'runtime' }">
          <PreviewFrame v-show="side === 'preview'" :status="preview.status.value" />
          <template v-if="side === 'files'">
            <v-btn-toggle v-model="fileView" mandatory color="primary" variant="outlined" divided
              density="comfortable" class="mb-3">
              <v-btn value="current">いまのファイル</v-btn>
              <v-btn value="changes">変更履歴</v-btn>
            </v-btn-toggle>
            <SourceBrowser v-if="fileView === 'current'" :project-id="project.id" :project-name="project.name"
              :active="side === 'files'" :generating="active" />
            <ChangeHistory v-else ref="changeList" :project-id="project.id"
              :active="side === 'files' && fileView === 'changes'" :read-only="readOnly"
              :can-administer="!!project.can_administer" />
          </template>
          <PreviewControls v-show="side === 'runtime'" :project-id="project.id" :jobs="jobs"
            :enabled="previewEnabled && !readOnly" :preview="preview" :active="side === 'runtime'"
            :shell-enabled="shellEnabled" :can-revalidate="enabled && !readOnly" :revalidating="loading" :revalidation="revalidation"
            @revalidate="revalidate" />
          <template v-if="side === 'spec'">
            <div class="spec-header">
              <div class="spec-title"><p class="eyebrow">SPECIFICATION / 第{{ project.revision }}版</p>
                <h2>{{ project.name }}</h2></div>
              <v-btn variant="outlined" :disabled="readOnly" @click="emit('editSpec')">仕様を修正</v-btn>
            </div>
            <v-tabs v-model="specSection" density="comfortable" class="mb-3 spec-sections">
              <v-tab v-if="project.creation_profile" value="profile" prepend-icon="mdi-chart-box-outline">作るもの</v-tab>
              <v-tab value="records" prepend-icon="mdi-table">データ</v-tab>
              <v-tab value="masters" prepend-icon="mdi-database-outline">マスター</v-tab>
              <v-tab value="requirements" prepend-icon="mdi-format-list-checks">確認した要件</v-tab>
            </v-tabs>
            <template v-if="specSection === 'profile' && project.creation_profile">
              <div class="profile-summary mb-3">
                <div class="profile-summary__item">
                  <span>作り方</span>
                  <strong>{{ appPatternLabels[project.creation_profile.app_pattern]
                    || appTypeLabels[project.creation_profile.app_type] }}</strong>
                </div>
                <div class="profile-summary__item">
                  <span>作るもの</span>
                  <div class="profile-goals">
                    <span v-for="goal in project.creation_profile.goals" :key="goal" class="chip chip--sm">
                      {{ goalLabels[goal] || goal }}</span>
                    <span v-if="project.creation_profile.other_goal" class="chip chip--sm">
                      {{ project.creation_profile.other_goal }}</span>
                    <span v-if="project.creation_profile.mode === 'prompt'" class="chip chip--sm">
                      依頼文で指定</span>
                  </div>
                </div>
                <div v-if="project.creation_profile.production_day_start" class="profile-summary__item">
                  <span>1日の始まり</span>
                  <strong>{{ project.creation_profile.production_day_start }}</strong>
                </div>
                <div v-if="project.creation_profile.production_day_hours" class="profile-summary__item">
                  <span>1日の稼働時間</span>
                  <strong>{{ dayHoursLabel(project.creation_profile.production_day_hours) }}</strong>
                </div>
                <div v-if="project.creation_profile.default_period" class="profile-summary__item">
                  <span>開いたときの期間</span>
                  <strong>{{ periodLabels[project.creation_profile.default_period] }}</strong>
                </div>
              </div>
              <div class="spec-command-panel mb-3">
                <div class="prompt-actions">
                  <v-btn v-if="!promptEditing" color="primary" variant="tonal" prepend-icon="mdi-pencil-outline"
                    :disabled="readOnly" @click="startPromptEdit">編集開始</v-btn>
                  <template v-else>
                    <v-btn variant="text" @click="cancelPromptEdit">キャンセル</v-btn>
                    <v-btn color="primary" prepend-icon="mdi-content-save-outline"
                      :disabled="readOnly || !promptValid || !promptChanged"
                      @click="emit('savePrompt', cleanPrompt)">保存</v-btn>
                    <span v-if="promptChanged" class="meta">未保存の変更があります。</span>
                  </template>
                </div>
                <div class="approval"><slot name="approval" /></div>
              </div>
              <div class="prompt-section mt-3">
                <h3>AIへの依頼文</h3>
                <p class="meta my-2">この内容をアプリ生成時にAIへ渡します。内部の開発ルールやスキルは省いています。</p>
                <v-textarea v-model="promptDraft" class="prompt-editor" aria-label="AIへの依頼文"
                  rows="10" maxlength="10000" hide-details
                  :readonly="!promptEditing || readOnly" />
                <p v-if="promptEditing && !promptValid" class="tip tip--warn mt-3">依頼文は5文字以上、10,000文字以内で入力してください。</p>
              </div>
            </template>
            <template v-else-if="specSection === 'records' || specSection === 'masters'">
              <p v-if="!specTables.length" class="tip mb-4">
                {{ specSection === 'masters' ? '登録されたマスターはありません。' : '登録されたデータはありません。' }}
              </p>
              <template v-else>
                <v-tabs v-model="specTable" show-arrows density="compact" class="mb-3 spec-tabs">
                  <v-tab v-for="(table, index) in specTables" :key="table.name" :value="index">{{ table.name }}</v-tab>
                </v-tabs>
                <v-table density="compact" class="spec-table">
                  <thead><tr><th>項目名</th><th>内容</th><th>必須</th></tr></thead>
                  <tbody><tr v-for="field in specTables[specTable]?.fields || []" :key="field.name">
                    <td>{{ field.name }}</td><td>{{ kinds[field.kind] }}</td>
                    <td>{{ field.required ? 'はい' : 'いいえ' }}</td></tr></tbody>
                </v-table>
              </template>
            </template>
            <template v-else-if="specSection === 'requirements'">
              <p class="tip mb-4">ヒアリングで整理した要件を修正できます。保存すると、仕様をもう一度確認する状態になります。</p>
              <div v-for="(_, index) in requirementsDraft" :key="index" class="requirement-row">
                <v-textarea v-model="requirementsDraft[index]" :label="`要件 ${index + 1}`"
                  rows="1" auto-grow maxlength="500" density="compact" />
                <v-btn icon="mdi-minus-circle-outline" variant="text" color="error"
                  :aria-label="`要件${index + 1}を削除`" @click="requirementsDraft.splice(index, 1)" />
              </div>
              <p v-if="!requirementsDraft.length" class="tip mb-3">確認した要件はまだありません。</p>
              <div class="requirement-actions">
                <v-btn variant="outlined" prepend-icon="mdi-plus"
                  :disabled="readOnly || requirementsDraft.length >= 40"
                  @click="requirementsDraft.push('')">要件を追加</v-btn>
                <v-btn color="primary" :disabled="readOnly || !requirementsValid || !requirementsChanged"
                  @click="emit('saveRequirements', cleanRequirements)">要件を保存</v-btn>
                <span v-if="requirementsChanged" class="meta">未保存の変更があります。</span>
              </div>
              <p v-if="!requirementsValid" class="tip tip--warn mt-3">同じ要件は重複できません。各要件は500文字以内にしてください。</p>
            </template>
          </template>
          <!-- 共有とテナントは仕様と別の話。仕様の下に積むと、読む順番が途切れる。 -->
          <div v-if="side === 'sharing'"><slot name="sharing" /></div>
          <template v-if="side === 'history'">
            <h2 class="mb-4">動作確認へ出した記録</h2>
            <p class="my-3">区切りとして残します。チャットでの生成1回ごとではありません。
              生成ごとの変更は下の「コードの変更履歴」にあります。</p>
            <v-alert v-if="historyError" type="warning" class="my-3">{{ historyError }}</v-alert>
            <p v-if="!history.length" class="tip">まだ動作確認へ出していません。</p>
            <div v-for="item in history" :key="item.at" class="record mb-5">
              <p><strong>{{ new Date(item.at).toLocaleString('ja-JP') }}</strong> に動作確認へ</p>
              <p class="meta">第{{ item.revision }}版（{{ new Date(item.generated_at).toLocaleString('ja-JP') }} 生成）</p>
              <p v-if="item.instruction" class="my-2">依頼：{{ item.instruction }}</p>
              <v-btn variant="outlined" size="small" class="mt-2"
                :href="`/api/projects/${project.id}/jobs/${item.job_id}/source`">このコードを取得</v-btn>
            </div>
            <v-btn variant="outlined" :disabled="loading" @click="loadHistory">記録を更新</v-btn>

            <p class="tip my-4">
              <v-icon icon="mdi-source-commit" />
              <span>生成1回ごとの変更と差分は、ファイルタブの「変更履歴」で見られます。</span>
            </p>

            <!-- 生成の検査・履歴を通らないコードが載る口。有効な環境でだけ出す。 -->
            <template v-if="localCodexEnabled">
            <v-divider class="my-6" />
            <h3>手元のCodexアプリで開発する</h3>
            <p class="my-3">いまのコード・仕様・規約が入った作業フォルダを取得し、普段のCodex画面で続きを開発します。完成したZIPをここへ戻します。</p>
            <v-btn variant="outlined" prepend-icon="mdi-download-outline" :loading="loading"
              :disabled="project.status !== 'approved'" @click="downloadPackage">作業パッケージを取得</v-btn>
            <v-alert v-if="localNotice" type="success" variant="tonal" closable class="mt-4"
              @click:close="localNotice = ''">{{ localNotice }}</v-alert>
            <div class="local-upload mt-5">
              <label class="file-label" for="local-artifact">完成したアプリのZIP</label>
              <input id="local-artifact" type="file" accept="application/zip,.zip" :disabled="loading"
                @change="event => localFile = (event.target as HTMLInputElement).files?.[0]" />
              <v-btn variant="outlined" :loading="loading"
                :disabled="!localFile || project.status !== 'approved'" @click="uploadLocal">ZIPを登録</v-btn>
            </div>
            <p class="tip mt-4">秘密情報、.env、node_modules、distはZIPへ含めないでください。</p>
            </template>
          </template>
        </div>
      </v-card>
    </div>
  </div>
</template>

<style scoped>
.panes { display: grid; gap: var(--sp-3); align-items: stretch; }
/* 掴む場所は細くてよいが、当たり判定は指でも届く幅にする。 */
.grip { cursor: col-resize; position: relative; border-radius: var(--radius-pill);
  background: var(--border-default); }
.grip::after { content: ""; position: absolute; inset: 0 calc(var(--sp-2) * -1); }
.grip:hover, .grip:focus-visible { background: var(--border-brand); outline: none; }
.pane { height: calc(100dvh - var(--forge-pane-offset)); min-height: var(--forge-pane-min);
  display: flex; flex-direction: column; }
.pane--side { padding: 0; }
.requirements-shell:not(:empty) { flex: 0 0 auto; max-height: 58%; overflow-y: auto; }
.side-body { flex: 1; overflow-y: auto; min-height: 0; display: flex; flex-direction: column; }
.side-body--profile { overflow: hidden; padding-bottom: var(--sp-2) !important; }
.side-body--profile .prompt-section { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; }
.side-body--runtime { overflow: hidden; padding-top: var(--sp-3) !important;
  padding-bottom: var(--sp-2) !important; }
.local-upload { display: flex; gap: var(--sp-4); align-items: center; flex-wrap: wrap; }
.file-label { color: var(--ink-2); font-weight: var(--fw-medium); }
.meta { color: var(--ink-4); font-size: var(--fs-xs); }
.record { border-left: 3px solid var(--border-brand); padding-left: var(--sp-4); }
.record-actions { display: flex; gap: var(--sp-2); flex-wrap: wrap; }
/* 差分は行頭の +/- で読む。折り返すと記号の位置が崩れるので、横に流す。 */
.diff { background: var(--surface-sunken); border: 1px solid var(--border-subtle);
  color: var(--ink-2); padding: var(--sp-4);
  border-radius: var(--radius-md); max-height: var(--forge-log-height); overflow: auto;
  white-space: pre; font-size: var(--fs-xs); }
.spec-tabs :deep(.v-tab) { font-size: var(--fs-xs); }
.spec-header { display: grid; grid-template-columns: minmax(0, 1fr) auto;
  align-items: center; gap: var(--sp-4); margin-bottom: var(--sp-2); }
.spec-title { min-width: 0; }
.spec-title .eyebrow { margin-bottom: var(--sp-1); }
.spec-title h2 { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.profile-summary { display: grid; grid-template-columns: minmax(0, 2fr) repeat(2, minmax(0, 1fr)); gap: var(--sp-3); }
.profile-summary__item { display: flex; flex-direction: column; align-items: flex-start; gap: var(--sp-1); padding: var(--sp-3); border: 1px solid var(--border-default); border-radius: var(--radius-md); background: var(--surface-sunken); }
.profile-summary__item > span { color: var(--ink-4); font-size: var(--fs-xs); }
.profile-summary__item strong { color: var(--ink-1); font-size: var(--fs-md); }
.profile-goals { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
.spec-command-panel { display: flex; align-items: center; gap: var(--sp-4); padding: var(--sp-3);
  border: 1px solid var(--border-default); border-radius: var(--radius-md);
  background: var(--surface-sunken); }
.prompt-actions { display: flex; flex: 0 0 auto; gap: var(--sp-3); align-items: center; flex-wrap: wrap; }
.approval { flex: 1 1 auto; min-width: 0; }
.prompt-editor { flex: 1 1 auto; min-height: 0; }
.prompt-editor :deep(.v-input__control),
.prompt-editor :deep(.v-field),
.prompt-editor :deep(.v-field__field) { height: 100%; min-height: 0; }
.prompt-editor :deep(.v-field__input) { height: 100%; min-height: 0;
  overflow-y: auto !important; resize: none; }
.spec-sections { flex: 0 0 auto; }
.spec-table :deep(th), .spec-table :deep(td) { font-size: var(--fs-xs); padding-block: var(--sp-2) !important; }
/* 入力欄は hideDetails: 'auto' で下の余白を持たない。行ごとに間を空けないと、
   次の行の浮いたラベルが前の行の枠線に重なる。 */
/* 入力欄は hideDetails: 'auto' で下の余白を持たない。行ごとに間を空けないと、
   次の行の浮いたラベルが前の行の枠線に重なる。 */
.requirement-row { display: grid; grid-template-columns: 1fr auto; gap: var(--sp-3);
  align-items: center; margin-bottom: var(--sp-3); }
.requirement-actions { display: flex; gap: var(--sp-3); align-items: center; flex-wrap: wrap; }
/* 中身が無いときに余白だけ残さない。 */
.approval:empty { display: none; }
.holder { display: flex; gap: var(--sp-4); align-items: center; justify-content: space-between; flex-wrap: wrap; }
@media (max-width: 1100px) { /* --forge-stack-width */
  /* 縦に積む幅では左右の調整はしない。掴む場所も消す。 */
  .panes { grid-template-columns: 1fr !important; }
  .grip { display: none; }
  .pane { height: auto; min-height: var(--forge-pane-min); }
  .spec-command-panel { align-items: stretch; flex-direction: column; }
  .spec-header { grid-template-columns: 1fr auto; }
  .side-body--profile { overflow-y: auto; }
  .side-body--runtime { overflow-y: auto; }
  .side-body--profile .prompt-section { flex: none; }
  .prompt-editor :deep(.v-field__input) { height: clamp(18rem, 50vh, 38rem); min-height: 18rem; }
}
@media (max-width: 600px) {
  .profile-summary { grid-template-columns: 1fr; }
  .spec-header { grid-template-columns: 1fr; }
  .spec-header > .v-btn { grid-column: 1; grid-row: auto; justify-self: start; }
}
</style>
