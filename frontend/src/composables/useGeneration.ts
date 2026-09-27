import { onUnmounted, ref } from 'vue'
import { api } from './useApi'
import type { GenerationJob } from '@/types'
import { APP_NAME } from '@/branding'

export function useGeneration(projectId: string) {
  const jobs = ref<GenerationJob[]>([]), loading = ref(false), error = ref('')
  let timer: ReturnType<typeof setTimeout> | undefined
  let disposed = false
  // 画面を開いたときに一度だけ、直近の失敗も実行環境へ確かめ直す。届かなかっただけで
  // 失敗になった記録は、実際にはまだ生成が続いていることがある。放っておくと
  // 進行中の生成を見る手立てが無くなり、二重に依頼してしまう。
  let recheckedFailure = false
  async function refresh() {
    clearTimeout(timer)
    try {
      jobs.value = await api<GenerationJob[]>(`/api/projects/${projectId}/jobs`)
      const running = jobs.value.filter(j => ['starting', 'generating'].includes(j.status))
      // 直近のものだけ。古い失敗は、いま動いている見込みがない。
      const recently = Date.now() - 2 * 60 * 60 * 1000
      const stale = recheckedFailure ? [] : jobs.value
        .filter(j => j.status === 'failed' && Date.parse(j.updated_at) > recently).slice(0, 5)
      recheckedFailure = true
      for (const job of [...running, ...stale]) {
        const current = await api<GenerationJob>(`/api/projects/${projectId}/jobs/${job.id}/refresh`, 'POST')
        jobs.value = jobs.value.map(j => j.id === current.id ? current : j)
      }
      // 一時的な準備中・通信失敗が解消したら、古い警告を画面に残さない。
      // 実行環境からの理由をそのまま出す。固定文に潰すと、準備中なのか
      // 容量・権限で作れないのかが分からず、待てば済むのか判断できない。
      const unconfirmed = jobs.value.find(j => j.unconfirmed)
      error.value = unconfirmed
        ? `${unconfirmed.unconfirmed_reason ?? '実行環境へ届きません。'}`
          + '（生成の状態を確認できていません。生成自体は続いている場合があります）'
        : ''
    } catch (e) { error.value = e instanceof Error ? e.message : '生成状態を確認できません。' }
    if (!disposed && jobs.value.some(j => ['starting', 'generating'].includes(j.status))) timer = setTimeout(refresh, 5000)
  }
  async function sendInstruction(text: string, choice: { model: string; effort: string; provider?: string; chat_id?: string }) {
    loading.value = true; error.value = ''
    try { await api<GenerationJob>(`/api/projects/${projectId}/messages`, 'POST', { text, ...choice }); await refresh(); return true }
    catch (e) { error.value = e instanceof Error ? e.message : '変更を依頼できません。'; return false }
    finally { loading.value = false }
  }
  async function cancel(jobId: string) {
    loading.value = true; error.value = ''
    try { await api<GenerationJob>(`/api/projects/${projectId}/jobs/${jobId}/cancel`, 'POST'); await refresh() }
    catch (e) { error.value = e instanceof Error ? e.message : '生成を止められません。' }
    finally { loading.value = false }
  }
  // 失敗・停止した生成を、作り直さずにもう一度検査する。通れば完了になる。
  // 通らなかったときは、何が足りないかを一覧で持っておき、画面に出す。
  const revalidation = ref<{ jobId: string; passed: boolean; problems: string[] }>()
  async function revalidate(jobId: string) {
    loading.value = true; error.value = ''; revalidation.value = undefined
    try {
      const result = await api<GenerationJob & { problems?: string[] }>(
        `/api/projects/${projectId}/jobs/${jobId}/revalidate`, 'POST')
      revalidation.value = { jobId, passed: result.status === 'generated', problems: result.problems ?? [] }
      await refresh()
    }
    catch (e) { error.value = e instanceof Error ? e.message : '再検査できませんでした。' }
    finally { loading.value = false }
  }
  async function generate(choice: { model: string; effort: string; provider?: string; chat_id?: string }) {
    loading.value = true; error.value = ''
    try { await api<GenerationJob>(`/api/projects/${projectId}/generate`, 'POST', choice); await refresh() }
    catch (e) { error.value = e instanceof Error ? e.message : '生成を開始できません。' }
    finally { loading.value = false }
  }
  async function downloadLocalPackage() {
    loading.value = true; error.value = ''
    try {
      const response = await fetch(`/api/projects/${projectId}/local-package`, {
        credentials: 'same-origin',
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({})) as { error?: string; detail?: string }
        throw new Error(data.error ?? data.detail ?? '作業パッケージを取得できませんでした。')
      }
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `${APP_NAME.toLowerCase()}-${projectId}.zip`
      anchor.style.display = 'none'
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000)
      return true
    } catch (e) {
      error.value = e instanceof Error ? e.message : '作業パッケージを取得できませんでした。'
      return false
    } finally { loading.value = false }
  }
  async function uploadLocalArtifact(file: File) {
    loading.value = true; error.value = ''
    try {
      const response = await fetch(`/api/projects/${projectId}/local-artifact`, {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/zip' }, body: file,
      })
      const data = await response.json() as GenerationJob & { error?: string }
      if (!response.ok) throw new Error(data.error ?? 'ZIPファイルを受け付けられませんでした。')
      await refresh()
      return data
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'ZIPファイルを受け付けられませんでした。'
    } finally { loading.value = false }
  }
  onUnmounted(() => { disposed = true; clearTimeout(timer) })
  return { jobs, loading, error, refresh, generate, sendInstruction, cancel, revalidate, revalidation, downloadLocalPackage, uploadLocalArtifact }
}
