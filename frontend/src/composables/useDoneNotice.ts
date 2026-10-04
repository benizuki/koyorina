import { onUnmounted, ref, watch } from 'vue'
import type { Ref } from 'vue'
import type { GenerationJob, PreviewStatus } from '@/types'

export type DoneNotice = { type: 'success' | 'error'; text: string }

/**
 * 生成とプレビューの「終わった」を知らせる。
 *
 * どちらも数分かかるため、待っている間は別の作業へ移る。画面の中に状態を出すだけでは
 * 気づかれない。手元を離れている間に終わったときは、タブの見出しにも印を付け、
 * 戻ってきたら元へ戻す。
 */
export function useDoneNotice(jobs: Ref<GenerationJob[]>, active: Ref<boolean>,
                              preview: Ref<PreviewStatus | undefined>) {
  const notice = ref<DoneNotice>()
  const shown = ref(false)
  const original = document.title
  let marked = false
  let activeJobId: string | undefined

  function restore() {
    if (!marked) return
    document.title = original
    marked = false
  }
  function announce(type: DoneNotice['type'], text: string, label: string) {
    notice.value = { type, text }
    shown.value = true
    // 画面を見ているなら見出しは触らない。すぐ戻す印を付けても読まれない。
    if (document.hasFocus()) return
    document.title = `${label} — ${original}`
    marked = true
  }
  // 隠れるときのvisibilitychangeで消さない。印が要るのは隠れている間そのもの。
  function restoreWhenVisible() { if (!document.hidden) restore() }

  watch(active, (now, before) => {
    // 次の依頼を出した時点で、前回の知らせは用済み。並べて出しても読み違えるだけ。
    if (now) {
      shown.value = false
      restore()
      activeJobId = jobs.value.find(job => ['starting', 'generating'].includes(job.status))?.id
      return
    }
    if (!before) return
    const job = activeJobId ? jobs.value.find(item => item.id === activeJobId) : undefined
    activeJobId = undefined
    if (!job || job.status === 'starting' || job.status === 'generating') return
    const kind = job.instruction ? '修正' : '作成'
    if (job.status === 'generated') {
      announce('success',
               `アプリの${kind}が完了しました。動かして確かめるには、実行管理タブで「プレビューを開始」を押してください。`,
               `✅ ${kind}完了`)
    } else {
      announce('error', job.error || `アプリの${kind}が完了しませんでした。`, `⚠️ ${kind}失敗`)
    }
  })

  watch(() => preview.value?.state, (now, before) => {
    // 最初の状態取得では知らせない。止めたのは自分なので停止も知らせない。
    if (!now || !before || now === before) return
    if (now === 'running') {
      announce('success', 'プレビューが起動しました。プレビュータブでアプリを操作できます。', '✅ 起動')
    } else if (now === 'failed') {
      announce('error', preview.value?.message || 'プレビューが異常終了しました。', '⚠️ 異常終了')
    }
  })

  window.addEventListener('focus', restore)
  document.addEventListener('visibilitychange', restoreWhenVisible)
  onUnmounted(() => {
    window.removeEventListener('focus', restore)
    document.removeEventListener('visibilitychange', restoreWhenVisible)
    restore()
  })
  return { notice, shown, announce }
}
