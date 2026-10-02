<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useGenerationProgress } from '@/composables/useGenerationProgress'
const props = defineProps<{ projectId: string; jobId: string; active: boolean }>()
const { progress, error, refreshing, refresh } = useGenerationProgress(props.projectId, props.jobId, computed(() => props.active))
const time = (value: string) => new Date(value).toLocaleString('ja-JP')
const now = ref(Date.now())
let clock: ReturnType<typeof setInterval> | undefined
onMounted(() => { clock = setInterval(() => { now.value = Date.now() }, 1000) })
onUnmounted(() => clearInterval(clock))
const received = computed(() => progress.value.response_bytes < 1024
  ? `${progress.value.response_bytes} B`
  : `${(progress.value.response_bytes / 1024).toFixed(1)} KB`)
const elapsed = computed(() => {
  const start = progress.value.events[0]?.at
  if (!start) return ''
  const seconds = Math.max(0, Math.floor((now.value - new Date(start).getTime()) / 1000))
  return `${Math.floor(seconds / 60)}分${seconds % 60}秒`
})
const eventLabel = (kind: string) => kind === 'codex' ? 'AppGen' : kind === 'file' ? 'ファイル変更'
  : kind === 'command' ? 'ローカル検査' : kind === 'plan' ? '作業計画' : 'システム'
const stateLabel = (state?: string | null) => state === 'running' ? '実行中'
  : state === 'failed' ? '未完了' : state === 'done' ? '完了' : ''
const eventIcon = (kind: string) => kind === 'codex' ? 'mdi-robot-outline' : kind === 'file' ? 'mdi-file-edit-outline'
  : kind === 'command' ? 'mdi-console-line' : kind === 'plan' ? 'mdi-format-list-checks' : kind === 'error'
    ? 'mdi-alert-circle-outline' : 'mdi-progress-clock'
</script>

<template>
  <details class="my-3" :open="active">
    <summary>AppGenの作業報告</summary>
    <p class="tip my-3">生成中は約2秒ごとに更新します。AppGenの報告、作業計画、ファイル変更、ローカル検査を表示します。同じ作業は1行が状態を変えていきます。コード本文・コマンド出力・内部の思考は表示しません。</p>
    <p v-if="progress.last_response_at" class="my-3">
      AppGen応答中：メッセージ受信量 {{ received }} ／ 最終受信 {{ time(progress.last_response_at) }}
    </p>
    <p v-else class="my-3">応答受信の記録はまだありません。</p>
    <p v-if="active && elapsed" class="my-3">開始からの経過時間：{{ elapsed }}</p>
    <v-alert v-if="error" type="warning" class="my-3">{{ error }}</v-alert>
    <p v-if="!progress.events.length" class="my-3">
      {{ active ? '依頼を受け付けました。実行環境からの作業報告を待っています。'
        : 'この生成の作業報告はありません。' }}
    </p>
    <p v-if="progress.truncated" class="my-3">直近200件を表示しています。</p>
    <v-list role="log" aria-label="AppGenの作業報告" aria-live="polite" class="progress-history">
      <v-list-item v-for="event in progress.events" :key="event.id" :prepend-icon="eventIcon(event.kind)">
        <v-list-item-subtitle>
          {{ time(event.at) }} ／ {{ eventLabel(event.kind) }}
          <span v-if="stateLabel(event.state)" class="chip chip--pill"
            :class="event.state === 'failed' ? 'chip--danger' : event.state === 'running' ? 'chip--warn' : 'chip--brand'">
            {{ stateLabel(event.state) }}
          </span>
        </v-list-item-subtitle>
        <v-list-item-title class="progress-message">{{ event.message }}</v-list-item-title>
      </v-list-item>
    </v-list>
    <v-btn variant="text" :loading="refreshing" @click="refresh">作業報告を再取得</v-btn>
  </details>
</template>

<style scoped>
.progress-history { max-height: var(--forge-log-height); overflow: auto; }
/* 会話の吹き出しより控えめにする。報告は読み流せる大きさでよい。 */
.progress-message { white-space: pre-wrap; overflow-wrap: anywhere; font-size: var(--fs-sm); }
.progress-history :deep(.v-list-item-subtitle) { font-size: var(--fs-2xs); }
</style>
