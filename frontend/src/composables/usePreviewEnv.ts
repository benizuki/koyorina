import { ref } from 'vue'
import { api } from './useApi'

/** プレビューへ渡す環境変数。
 *
 *  secret の項目はサーバーから値が返らない（configured で設定済みかだけ分かる）。
 *  入れ替えるときだけ value を送り、触らない項目は value を省いて送り返す。
 */
export interface EnvEntry {
  name: string; value: string | null; secret: boolean; configured: boolean
  // テナントの設定（生成アプリ用のGemini）から受け継いだ項目。保存の対象ではない。
  inherited?: boolean; overridden?: boolean
}

export function usePreviewEnv(projectId: string) {
  const entries = ref<EnvEntry[]>([]), inherited = ref<EnvEntry[]>([])
  const loading = ref(false), saving = ref(false)
  const error = ref(''), saved = ref(false)
  const path = `/api/projects/${projectId}/preview/env`

  async function refresh() {
    loading.value = true; error.value = ''
    try { split((await api<{ entries: EnvEntry[] }>(path)).entries) }
    catch (e) { error.value = e instanceof Error ? e.message : '環境変数を取得できません。' }
    finally { loading.value = false }
  }

  /** 受け継いだ項目は見せるだけにして、このアプリの項目とは別に持つ。 */
  function split(all: EnvEntry[]) {
    inherited.value = all.filter(entry => entry.inherited)
    entries.value = all.filter(entry => !entry.inherited)
  }

  function add() {
    entries.value = [...entries.value, { name: '', value: '', secret: false, configured: false }]
  }
  function remove(index: number) {
    entries.value = entries.value.filter((_, i) => i !== index)
  }
  /** 秘密に切り替えたら、画面に残っている値は入力し直してもらう。 */
  function setSecret(index: number, secret: boolean) {
    const next = [...entries.value]
    next[index] = { ...next[index], secret, value: '', configured: secret ? next[index].configured : true }
    entries.value = next
  }

  async function save() {
    saving.value = true; error.value = ''; saved.value = false
    const payload = entries.value.map(entry => (
      // 設定済みの秘密を空のまま送ると「消す」意味になってしまう。値を省いて前の値を残す。
      entry.secret && entry.configured && !entry.value
        ? { name: entry.name, secret: true }
        : { name: entry.name, value: entry.value ?? '', secret: entry.secret }))
    try {
      const body = await api<{ entries: EnvEntry[] }>(path, 'PUT', { entries: payload })
      split(body.entries)
      saved.value = true
      return true
    } catch (e) {
      error.value = e instanceof Error ? e.message : '環境変数を保存できません。'
      return false
    } finally { saving.value = false }
  }

  return { entries, inherited, loading, saving, error, saved, refresh, add, remove, setSecret, save }
}
