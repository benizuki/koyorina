import { onUnmounted, ref } from 'vue'
import { api } from './useApi'
import type { PreviewStatus } from '@/types'

export function usePreview(projectId: string) {
  const status = ref<PreviewStatus>(), logs = ref(''), loading = ref(false), error = ref('')
  let timer: ReturnType<typeof setTimeout> | undefined
  let logTimer: ReturnType<typeof setTimeout> | undefined
  let disposed = false

  async function refresh() {
    clearTimeout(timer)
    try {
      status.value = await api<PreviewStatus>(`/api/projects/${projectId}/preview`)
      // 未起動は正常なstopped状態。以前の一時エラーが直ったら警告を消す。
      error.value = ''
    }
    catch (e) { error.value = e instanceof Error ? e.message : 'プレビューの状態を確認できません。'; return }
    // 起動中は細かく、それ以外もゆっくり見に行く。落ちたことに気付けるようにする。
    if (!disposed) timer = setTimeout(refresh, status.value?.state === 'starting' ? 4000 : 15000)
  }
  async function act(path: string, method: string, message: string) {
    loading.value = true; error.value = ''
    try { status.value = await api<PreviewStatus>(path, method) }
    catch (e) { error.value = e instanceof Error ? e.message : message }
    finally { loading.value = false }
    await refresh()
  }
  const start = (jobId: string) => act(`/api/projects/${projectId}/jobs/${jobId}/preview`, 'POST', 'プレビューを開始できません。')
  const restart = () => act(`/api/projects/${projectId}/preview/restart`, 'POST', 'プレビューを再起動できません。')
  const stop = () => act(`/api/projects/${projectId}/preview`, 'DELETE', 'プレビューを停止できません。')
  const reset = () => act(`/api/projects/${projectId}/reset`, 'POST', 'プロジェクトをリセットできません。')
  async function loadLogs() {
    clearTimeout(logTimer)
    try { logs.value = (await api<{ logs: string }>(`/api/projects/${projectId}/preview/logs`)).logs }
    catch (e) { error.value = e instanceof Error ? e.message : 'ログを取得できません。' }
    // 表示している間だけ追いかける。停止は watchLogs(false) で行う。
    if (!disposed && watching) logTimer = setTimeout(loadLogs, 5000)
  }
  let watching = false
  function watchLogs(active: boolean) {
    watching = active
    clearTimeout(logTimer)
    if (active) loadLogs()
  }
  onUnmounted(() => { disposed = true; clearTimeout(timer); clearTimeout(logTimer) })
  return { status, logs, loading, error, refresh, start, restart, stop, reset, loadLogs, watchLogs }
}
