import { ref } from 'vue'
import { api } from './useApi'

type Listing = { status: string; files: string[]; truncated: boolean }
type Content = { status: string; path: string; text?: string; reason?: string }

/** 生成されたファイルの一覧と中身。作業場所の「いまの状態」を読む。 */
export function useSources(projectId: string) {
  const files = ref<string[]>([]), truncated = ref(false)
  const selected = ref(''), text = ref(''), loading = ref(false), error = ref(''), downloading = ref(false)

  async function refresh() {
    loading.value = true; error.value = ''
    try {
      const result = await api<Listing>(`/api/projects/${projectId}/files`)
      files.value = result.files ?? []
      truncated.value = !!result.truncated
      // 選んでいたファイルが無くなっていたら、選択を外す。中身だけ残すと嘘になる。
      if (selected.value && !files.value.includes(selected.value)) { selected.value = ''; text.value = '' }
      else if (selected.value) await open(selected.value)
    } catch (e) { error.value = e instanceof Error ? e.message : 'ファイルを取得できません。' }
    finally { loading.value = false }
  }

  async function open(path: string) {
    loading.value = true; error.value = ''
    selected.value = path
    try {
      const result = await api<Content>(`/api/projects/${projectId}/files?path=${encodeURIComponent(path)}`)
      text.value = result.status === 'read' ? (result.text ?? '') : ''
      if (result.status !== 'read') error.value = result.reason ?? 'このファイルは表示できません。'
    } catch (e) { error.value = e instanceof Error ? e.message : 'ファイルを取得できません。' }
    finally { loading.value = false }
  }

  /** ファイルタブに出ているものを一式、ZIPで保存する。名前はアプリ名から作る。 */
  async function download(name: string) {
    downloading.value = true; error.value = ''
    try {
      const response = await fetch(`/api/projects/${projectId}/files/archive`, { credentials: 'same-origin' })
      if (!response.ok) {
        const data = await response.json().catch(() => ({})) as { error?: string; detail?: string }
        throw new Error(data.error ?? data.detail ?? 'ファイルをダウンロードできませんでした。')
      }
      const url = URL.createObjectURL(await response.blob())
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `${name.replace(/[\\/:*?"<>|\s]+/g, '_') || 'app'}.zip`
      anchor.style.display = 'none'
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000)
    } catch (e) { error.value = e instanceof Error ? e.message : 'ファイルをダウンロードできませんでした。' }
    finally { downloading.value = false }
  }

  return { files, truncated, selected, text, loading, error, downloading, refresh, open, download }
}
