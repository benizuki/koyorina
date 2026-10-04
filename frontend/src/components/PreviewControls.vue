<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import type { usePreview } from '@/composables/usePreview'
import { usePreviewEnv } from '@/composables/usePreviewEnv'
import { usePreviewShell } from '@/composables/usePreviewShell'
import { useClipboard } from '@/composables/useClipboard'
import { useLogLines } from '@/composables/useLogLines'
import type { GenerationJob } from '@/types'
import { generationStamp } from '@/generationLabels'
import { APP_NAME } from '@/branding'
const props = defineProps<{
  projectId: string; jobs: GenerationJob[]; enabled: boolean; active: boolean
  preview: ReturnType<typeof usePreview>
  canRevalidate?: boolean; revalidating?: boolean
  // コマンドで調べる口。無効な環境では見出しごと出さない（APIも断る）。
  shellEnabled?: boolean
  revalidation?: { jobId: string; passed: boolean; problems: string[] }
}>()
const emit = defineEmits<{ revalidate: [jobId: string] }>()
const { status, logs, loading, error, refresh, start, restart, stop, reset, watchLogs } = props.preview
const confirmDiscard = ref(false), picking = ref(false)
const logView = ref<HTMLElement>()
// ログは常に出す。押さないと見えない情報は、困っている人ほど辿り着けない。
// 追いかけるのはこのタブを見ている間だけ。隠れている画面のために取り続けない。
const lines = useLogLines(logs)
// ログはそのまま貼って相談・依頼できるように、行の色分けを抜いた元の文面でコピーする。
const logCopy = useClipboard(), outputCopy = useClipboard()
const copyLogs = () => logCopy.copy((logs.value ?? '').replace(/\s+$/, ''))
const copyOutput = () => {
  const result = shellResult.value
  if (!result) return
  outputCopy.copy([`$ ${result.command}`, result.stdout, result.stderr].filter(Boolean).join('\n').replace(/\s+$/, ''))
}
watch(() => props.active && props.enabled, on => watchLogs(on), { immediate: true })
// 追記されたら末尾へ送る。古い行を見続けることにならないようにする。
watch(logs, async () => {
  await nextTick()
  logView.value?.scrollTo({ top: logView.value.scrollHeight })
})
const generated = computed(() => props.jobs.filter(j => j.status === 'generated'))
const labels = { stopped: '停止中', starting: '起動中', running: '実行中', failed: '異常終了' }
const running = computed(() => status.value?.state === 'running')
const current = computed(() => generated.value.find(j => j.id === status.value?.job_id))
// 一覧はcreated_at降順。重ねて表示する選択メニューは使わず、1クリックで最新版を起動する。
const newest = computed(() => generated.value[0])
// 完了したコードが無く、最新の生成が失敗・停止しているなら、作業場所の検査だけやり直せる。
// 作業場所には最新の生成の中身しか無いので、対象は最新の1件に限る（一覧は新しい順）。
const retryTarget = computed(() => {
  const latest = props.jobs[0]
  return latest?.status === 'failed' && latest.source_type === 'managed_codex' ? latest : undefined
})
// 通ったときも知らせる。ボタンが黙って消えると、検査したのかどうか分からない。
const accepted = computed(() => props.revalidation?.passed
  && generated.value.some(job => job.id === props.revalidation?.jobId) ? props.revalidation : undefined)
const rejected = computed(() => props.revalidation && !props.revalidation.passed
  && props.revalidation.jobId === retryTarget.value?.id ? props.revalidation : undefined)
// アイコンだけでは「開始」と「最新で入れ替え」の違いが出せない。説明で補う。
const startLabel = computed(() =>
  running.value || status.value?.state === 'starting' ? '最新のコードで起動' : 'プレビューを開始')
async function launch(id: string) { picking.value = false; await start(id) }
const env = usePreviewEnv(props.projectId)
const showEnv = ref(false)
// 保存しただけでは動いているアプリに届かない。再起動して初めて反映される。
const envPending = computed(() => env.saved.value && running.value)
function toggleEnv() {
  showEnv.value = !showEnv.value
  if (showEnv.value) env.refresh()
}
async function saveEnv() {
  if (await env.save() && running.value) await restart()
}
// 調査用のコマンド実行。既定は施錠。解錠してから使う。
const shell = usePreviewShell(props.projectId)
const shellResult = shell.result
const command = ref('')
const confirmUnlock = ref(false)
async function send() {
  if (await shell.run(command.value)) command.value = ''
}
// 止まっている間は実行できない。閉じておけば、押してから断られずに済む。
watch(running, on => { if (!on) shell.lock() })
// 無効な環境で解錠状態が残らないようにする。
watch(() => props.shellEnabled, on => { if (!on) shell.lock() })
onMounted(refresh)
</script>

<template>
  <section class="runtime-controls">
    <v-alert v-if="!enabled" type="info" variant="tonal" class="my-4">
      プレビュー実行環境が無効です。ローカル検証時のみ利用できます。
    </v-alert>
    <template v-else>
      <v-alert v-if="error" type="error" class="my-4">{{ error }}</v-alert>
      <v-card class="runtime-panel">
        <div class="runtime-status-line">
          <span class="chip chip--pill" :class="running ? 'chip--brand' : status?.state === 'failed' ? 'chip--danger' : 'chip--warn'">
            {{ labels[status?.state ?? 'stopped'] }}
          </span>
          <span v-if="current">実行中のコード：{{ generationStamp(current) }}</span>
          <span v-if="running" class="meta">プレビュータブで画面を確認できます。</span>
        </div>
        <p v-if="status?.message" class="tip my-3"
          :class="status.state === 'failed' ? 'tip--danger' : 'tip--warn'">
          <v-icon :icon="status.state === 'failed' ? 'mdi-alert-circle-outline' : 'mdi-information-outline'" />
          <span>{{ status.message }}</span>
        </p>
        <p v-if="status?.hint" class="my-3">{{ status.hint }}</p>
        <p v-if="accepted" class="tip tip--brand my-3">
          <v-icon icon="mdi-check-circle-outline" />
          <span>作業場所の検査を通りました。生成し直さずに完了にしたので、プレビューを開始できます。</span>
        </p>
        <div v-if="!newest && retryTarget && canRevalidate" class="revalidate-note my-3">
          <p class="meta">止まった生成で書けているファイルを、生成し直さずにもう一度検査します。
            通れば生成完了になり、プレビューを開始できます。</p>
          <div v-if="rejected" class="tip tip--danger mt-2 rejected">
            <strong>検査を通りませんでした（{{ rejected.problems.length }}件）</strong>
            <ul v-if="rejected.problems.length"><li v-for="(problem, index) in rejected.problems" :key="index">
              <code>{{ problem }}</code></li></ul>
            <span v-else>詳細は生成の進捗を確認してください。</span>
          </div>
        </div>
        <pre v-if="status?.evidence" class="evidence my-3">{{ status.evidence }}</pre>
        <v-progress-linear v-if="status?.state === 'starting'" indeterminate color="primary" class="my-3" />
        <div class="actions mt-3">
          <!-- 起動・再起動・停止・更新はアイコン。毎回使う4つを、文字で横へ
               並べると行が長くなり、破壊的な「作業ディスクを削除」まで同じ
               見た目で並んでしまう。押し間違えたくないものほど形で分ける。
               アイコンだけだと意味が伝わらないので、tooltip と aria-label を必ず添える。 -->
          <div v-if="newest" class="run-actions">
            <v-tooltip :text="startLabel" location="top">
              <template #activator="{ props: tip }">
                <v-btn v-bind="tip" icon="mdi-play" color="primary" variant="flat"
                  :loading="loading" :aria-label="startLabel" @click="launch(newest.id)" />
              </template>
            </v-tooltip>
            <v-tooltip text="再起動" location="top">
              <template #activator="{ props: tip }">
                <v-btn v-bind="tip" icon="mdi-restart" variant="outlined"
                  :disabled="loading || !status?.job_id" aria-label="再起動" @click="restart" />
              </template>
            </v-tooltip>
            <v-tooltip text="停止" location="top">
              <template #activator="{ props: tip }">
                <v-btn v-bind="tip" icon="mdi-stop" variant="outlined"
                  :disabled="loading || status?.state === 'stopped'" aria-label="停止" @click="stop" />
              </template>
            </v-tooltip>
            <v-tooltip text="状態を更新" location="top">
              <template #activator="{ props: tip }">
                <v-btn v-bind="tip" icon="mdi-refresh" variant="outlined" :disabled="loading"
                  aria-label="状態を更新" @click="refresh" />
              </template>
            </v-tooltip>
          </div>
          <p v-else class="tip">生成が完了したコードがありません。</p>
          <v-btn v-if="!newest && retryTarget && canRevalidate" color="primary" variant="tonal"
            prepend-icon="mdi-shield-refresh-outline" :loading="revalidating" :disabled="revalidating"
            @click="emit('revalidate', retryTarget.id)">作業場所を検査して完了にする</v-btn>
          <v-btn v-if="generated.length > 1" variant="outlined" :disabled="loading" @click="picking = !picking">
            {{ picking ? '版の選択を閉じる' : '別の版を選ぶ' }}
          </v-btn>
          <v-btn variant="outlined" :disabled="loading" @click="toggleEnv">{{ showEnv ? '環境変数を閉じる' : '環境変数' }}</v-btn>
          <v-spacer />
          <!-- 元に戻せない操作。上の並びから離し、文字を残す。 -->
          <v-btn variant="outlined" color="error" prepend-icon="mdi-delete-outline"
            :disabled="loading || !generated.length" @click="confirmDiscard = true">最初から作り直す</v-btn>
        </div>
        <div v-if="picking" class="versions mt-4">
          <v-btn v-for="job in generated" :key="job.id" variant="outlined"
            :disabled="loading" @click="launch(job.id)">{{ generationStamp(job) }}</v-btn>
        </div>
        <template v-if="showEnv">
          <div class="env mt-4">
            <h3>環境変数</h3>
            <p class="meta my-2">
              生成アプリへ渡します。{{ APP_NAME }}が設定する項目（接続先・認証の受け渡しなど）は指定できません。
              保存すると実行中のプレビューを再起動して反映します。
            </p>
            <v-alert v-if="env.error.value" type="error" density="compact" class="my-3">{{ env.error.value }}</v-alert>
            <v-progress-linear v-if="env.loading.value" indeterminate color="primary" class="my-3" />
            <!-- テナントで用意したGemini。何もしなくても届く。同じ名前を下に足すと、このアプリだけ上書きする。 -->
            <div v-if="env.inherited.value.length" class="inherited my-3">
              <p class="meta">テナントの設定から受け継いでいる項目（同じ名前を追加すると、このアプリだけ上書きします）</p>
              <div class="inherited-list">
                <span v-for="item in env.inherited.value" :key="item.name" class="chip chip--sm"
                  :class="{ 'chip--warn': item.overridden }">
                  <code>{{ item.name }}</code>
                  <span v-if="item.overridden">上書き中</span>
                  <span v-else-if="item.secret">設定済み</span>
                  <span v-else>{{ item.value }}</span>
                </span>
              </div>
            </div>
            <p v-else-if="!env.entries.value.length" class="tip my-3">まだ設定がありません。</p>
            <div v-for="(item, index) in env.entries.value" :key="index" class="env-row">
              <v-text-field v-model="item.name" label="名前" placeholder="API_BASE" density="compact"
                class="env-name" :disabled="env.saving.value" />
              <v-text-field v-model="item.value" label="値" density="compact" class="env-value"
                :type="item.secret ? 'password' : 'text'" :disabled="env.saving.value"
                :placeholder="item.secret && item.configured ? '設定済み（変更するときだけ入力）' : ''" />
              <v-checkbox :model-value="item.secret" label="秘密" density="compact" class="env-secret"
                :disabled="env.saving.value" @update:model-value="env.setSecret(index, !!$event)" />
              <v-btn icon="mdi-close" variant="text" size="small" :disabled="env.saving.value"
                aria-label="この項目を削除" @click="env.remove(index)" />
            </div>
            <p class="meta my-2">
              「秘密」にした項目は保存後に画面へ返しません。入れ替えるときだけ入力してください。
            </p>
            <div class="actions mt-3">
              <v-btn variant="outlined" :disabled="env.saving.value" @click="env.add()">項目を追加</v-btn>
              <v-btn color="primary" :loading="env.saving.value" @click="saveEnv">
                {{ running ? '保存して再起動' : '保存' }}
              </v-btn>
              <span v-if="envPending" class="meta">反映のため再起動しました。</span>
              <span v-else-if="env.saved.value" class="meta">保存しました。次回の起動から反映されます。</span>
            </div>
          </div>
        </template>
        <div class="log-head mt-5">
          <h3>実行ログ</h3>
          <span class="legend"><i class="dot dot--error" />エラー</span>
          <span class="legend"><i class="dot dot--warn" />注意</span>
          <span class="legend"><i class="dot dot--ready" />起動完了</span>
          <v-btn variant="text" size="x-small" class="ml-auto" :disabled="!lines.length"
            :prepend-icon="logCopy.copied.value ? 'mdi-check' : 'mdi-content-copy'" @click="copyLogs">
            {{ logCopy.copied.value ? 'コピーしました' : 'ログをコピー' }}</v-btn>
        </div>
        <p v-if="logCopy.error.value" class="tip tip--warn tip--compact mt-2">{{ logCopy.error.value }}</p>
        <pre ref="logView" class="logs runtime-logs mt-2"><span v-for="line in lines" :key="line.key" class="line" :class="`line--${line.kind}`">{{ line.text }}</span><span v-if="!lines.length" class="line">まだ出力はありません。起動すると、実行中のアプリの出力がここに流れます。</span></pre>
        <p class="meta">5秒ごとに自動更新しています。赤い行が原因に近いところです。</p>

        <!-- 調査用。普段は施錠しておく。入力欄が常に開いていると、
             調べるつもりのない人が何となく打ってしまう。 -->
        <template v-if="shellEnabled">
        <div class="log-head mt-5">
          <h3>コマンドで調べる</h3>
          <span v-if="shell.unlocked.value" class="chip chip--warn chip--sm">解錠中</span>
        </div>
        <div v-if="!running" class="tip tip--compact mt-2">
          <v-icon icon="mdi-lock-outline" />
          <span>プレビューが動いているときだけ使えます。</span>
        </div>
        <div v-else-if="!shell.unlocked.value" class="tip tip--compact mt-2 shell-locked">
          <v-icon icon="mdi-lock-outline" />
          <span>実行中のアプリの中で、確認のためのコマンドを1つずつ実行できます。</span>
          <v-btn variant="outlined" size="small" @click="confirmUnlock = true">ロックを解除</v-btn>
        </div>
        <template v-else>
          <v-alert v-if="shell.error.value" type="error" density="compact" class="mt-2">
            {{ shell.error.value }}</v-alert>
          <div class="shell-bar mt-2">
            <v-text-field v-model="command" density="compact" hide-details
              placeholder="例：ls -la backend" prepend-inner-icon="mdi-chevron-right"
              :disabled="shell.running.value" @keydown.enter.prevent="send" />
            <v-btn color="primary" :loading="shell.running.value"
              :disabled="!command.trim()" @click="send">実行</v-btn>
            <v-btn variant="outlined" @click="shell.lock()">施錠</v-btn>
          </div>
          <!-- 1回ごとに立ち上げ直すので、cd も環境変数も次へ残らない。
               これを書いておかないと「cd したのに戻っている」で止まる。 -->
          <p class="meta mt-1">毎回 /workspace から始まります。移動して実行するときは
            <code>cd frontend &amp;&amp; npm run build</code> のように1行でつなげてください。</p>
          <div v-if="shellResult" class="result mt-3">
            <div class="result-head">
              <code>$ {{ shellResult.command }}</code>
              <span class="chip chip--sm" :class="shellResult.exit_code === 0 ? 'chip--brand' : 'chip--danger'">
                {{ shellResult.exit_code === null ? '終了コード不明' : `終了 ${shellResult.exit_code}` }}</span>
              <v-btn variant="text" size="x-small" class="ml-auto"
                :disabled="!shellResult.stdout && !shellResult.stderr"
                :prepend-icon="outputCopy.copied.value ? 'mdi-check' : 'mdi-content-copy'" @click="copyOutput">
                {{ outputCopy.copied.value ? 'コピーしました' : '結果をコピー' }}</v-btn>
            </div>
            <p v-if="outputCopy.error.value" class="tip tip--warn tip--compact mt-2">{{ outputCopy.error.value }}</p>
            <pre v-if="shellResult.stdout" class="logs mt-2">{{ shellResult.stdout }}</pre>
            <pre v-if="shellResult.stderr" class="logs logs--err mt-2">{{ shellResult.stderr }}</pre>
            <p v-if="!shellResult.stdout && !shellResult.stderr" class="meta">出力はありませんでした。</p>
          </div>
        </template>
        </template>
      </v-card>
      <p class="tip">開発中のアプリです。実データや秘密情報を入力しないでください。</p>
    </template>
    <v-dialog v-model="confirmUnlock" max-width="560"><v-card class="pa-6">
      <h2>コマンド実行のロックを解除しますか？</h2>
      <p class="my-4">実行中のアプリの中でコマンドを動かせるようになります。
        届く範囲は<strong>このプレビューの作業ディスクとプレビュー用データベースだけ</strong>で、
        生成の実行環境や他のアプリには影響しません。
        <strong>実行したコマンドは記録に残ります。</strong>
        画面を離れると、またロックされます。</p>
      <v-card-actions>
        <v-btn variant="outlined" @click="confirmUnlock = false">キャンセル</v-btn>
        <v-btn color="primary" @click="confirmUnlock = false; shell.unlock()">解除する</v-btn>
      </v-card-actions>
    </v-card></v-dialog>
    <v-dialog v-model="confirmDiscard" max-width="520"><v-card class="pa-6">
      <h2>プロジェクトを最初から作り直しますか？</h2>
      <p class="my-4">生成したコード、生成履歴、プレビュー用データベース、導入済みの依存関係を削除します。仕様とプロジェクト自体は残ります。元に戻せません。</p>
      <v-card-actions>
        <v-btn @click="confirmDiscard = false">キャンセル</v-btn>
        <v-btn color="error" @click="confirmDiscard = false; reset()">最初から作り直す</v-btn>
      </v-card-actions>
    </v-card></v-dialog>
  </section>
</template>

<style scoped>
.runtime-controls { height: 100%; min-height: 0; display: flex; flex-direction: column; }
/* 通知は縮めない。縦並びで下のパネルが高さを取ると、潰れて文面が読めなくなる。 */
.runtime-controls > .v-alert { flex: 0 0 auto; }
.runtime-panel { flex: 1 1 auto; min-height: 0; overflow-y: auto; padding: var(--sp-4); }
.rejected { display: flex; flex-direction: column; align-items: stretch; gap: var(--sp-2); }
.rejected ul { margin: 0; padding-left: var(--sp-5); }
.rejected code { font-size: var(--fs-xs); white-space: pre-wrap; overflow-wrap: anywhere; }
.runtime-status-line { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; }
.actions { display: flex; gap: var(--sp-3); align-items: center; flex-wrap: wrap; }
/* アイコンは地続きにしない。並びの中で隣と接していると、押し間違える。
   起動だけ少し離し、その先の再起動・停止・更新と役割を分ける。 */
.run-actions { display: flex; gap: var(--sp-2); align-items: center; }
.run-actions > :first-child { margin-right: var(--sp-2); }
.versions { display: flex; gap: var(--sp-3); flex-wrap: wrap; }
.meta { color: var(--ink-4); font-size: var(--fs-xs); }
.inherited-list { display: flex; flex-wrap: wrap; gap: var(--sp-2); margin-top: var(--sp-2); }
.inherited-list .chip { gap: var(--sp-1); }
.env-row { display: flex; gap: var(--sp-3); align-items: flex-start; flex-wrap: wrap; margin-bottom: var(--sp-2); }
.env-name { max-width: 16rem; }
.env-value { min-width: 18rem; flex: 1; }
.env-secret { flex: 0 0 auto; }
.evidence { background: var(--surface-sunken); border: 1px solid var(--border-subtle);
  color: var(--ink-2); padding: var(--sp-3);
  border-radius: var(--radius-md); font-size: var(--fs-xs); white-space: pre-wrap; word-break: break-all; }
.logs { background: var(--surface-sunken); color: var(--ink-2); padding: var(--sp-4);
  border: 1px solid var(--border-subtle); border-radius: var(--radius-md);
  max-height: var(--forge-log-height); overflow: auto; white-space: pre-wrap; word-break: break-all; font-size: var(--fs-xs); }
.runtime-logs { height: clamp(18rem, 42dvh, 34rem); max-height: none; }
.log-head { display: flex; gap: var(--sp-3); align-items: baseline; flex-wrap: wrap; }
.shell-locked { justify-content: space-between; }
.shell-bar { display: flex; gap: var(--sp-2); align-items: center; }
.shell-bar :deep(.v-field__input) { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.result-head { display: flex; gap: var(--sp-3); align-items: center; flex-wrap: wrap; }
.result-head code { font-size: var(--fs-sm); color: var(--ink-1); }
/* 標準エラーは左の線でも分かるようにする。色だけに頼らない。 */
.logs--err { color: var(--text-danger); box-shadow: inset 2px 0 var(--text-danger); }
.legend { display: inline-flex; gap: var(--sp-2); align-items: center; color: var(--ink-3);
  font-size: var(--fs-sm); }
.dot { width: 10px; height: 10px; border-radius: var(--radius-pill); flex: none; }
.dot--error { background: var(--text-danger); }
.dot--warn  { background: var(--text-warn); }
.dot--ready { background: var(--text-brand); }
/* 色だけに頼らない。エラーは左の線でも分かるようにする。 */
.line { display: block; }
/* 空行も1行として残す。詰めると、まとまりの切れ目が分からなくなる。 */
.line:empty::before { content: "\00a0"; }
.line--error { color: var(--text-danger); box-shadow: inset 2px 0 var(--text-danger); padding-left: var(--sp-2); }
.line--warn  { color: var(--text-warn); }
.line--ready { color: var(--text-brand); }
.line--info  { color: var(--ink-4); }
@media (max-width: 1100px) {
  .runtime-controls { height: auto; }
  .runtime-panel { overflow-y: visible; }
}
</style>
