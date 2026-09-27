import { ref } from 'vue'
import { api } from './useApi'
import type { CodeHistoryDiff, CodeHistoryEntry } from '@/types'

/**
 * 生成コードの変更履歴。生成1回ごとに1件ある。
 *
 * 「動作確認へ出した記録」とは別物。あちらは区切り（リリース）で、こちらは
 * 変更そのもの。プレビューへ出していない生成もここには残る。
 */
export function useCodeHistory(projectId: string) {
  const entries = ref<CodeHistoryEntry[]>([])
  const diff = ref<CodeHistoryDiff>()
  const loading = ref(false), error = ref('')
  let opened = ''

  async function refresh() {
    loading.value = true; error.value = ''
    try { entries.value = (await api<{ entries: CodeHistoryEntry[] }>(
      `/api/projects/${projectId}/history`)).entries }
    catch (e) { error.value = e instanceof Error ? e.message : '変更履歴を取得できません。' }
    finally { loading.value = false }
  }
  async function show(commit: string) {
    // もう一度押したら閉じる。並べて開くと、どれの差分か分からなくなる。
    if (opened === commit) { opened = ''; diff.value = undefined; return }
    loading.value = true; error.value = ''
    try {
      diff.value = await api<CodeHistoryDiff>(`/api/projects/${projectId}/history/${commit}`)
      opened = commit
    } catch (e) { error.value = e instanceof Error ? e.message : '差分を取得できません。' }
    finally { loading.value = false }
  }
  // 作業場所をその時点へ戻す。履歴は書き換えず、戻した結果を新しい1件として足す。
  // だから「戻したこと」自体もあとから取り消せる。
  async function restore(commit: string) {
    loading.value = true; error.value = ''
    try {
      await api(`/api/projects/${projectId}/history/${commit}/restore`, 'POST')
      await refresh()
      return true
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'この時点へ戻せません。'
      return false
    } finally { loading.value = false }
  }
  // 履歴そのものを消す。取り消せない。
  async function discard() {
    loading.value = true; error.value = ''
    try {
      await api(`/api/projects/${projectId}/history`, 'DELETE')
      entries.value = []; diff.value = undefined; opened = ''
      return true
    } catch (e) {
      error.value = e instanceof Error ? e.message : '履歴を消せません。'
      return false
    } finally { loading.value = false }
  }
  return { entries, diff, loading, error, refresh, show, restore, discard }
}
