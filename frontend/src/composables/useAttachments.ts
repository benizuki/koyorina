import { ref } from 'vue'
import { api } from './useApi'

export type Attachment = { name: string; bytes: number; kind?: string }

export async function uploadAttachment(projectId: string, file: File): Promise<Attachment> {
  const response = await fetch(`/api/projects/${projectId}/attachments`, {
    method: 'POST', credentials: 'same-origin', body: file,
    headers: { 'Content-Type': 'application/octet-stream', 'x-file-name': encodeURIComponent(file.name) },
  })
  const data = await response.json().catch(() => null)
  if (!response.ok || !data) throw new Error(data?.error ?? '添付できませんでした。')
  return data as Attachment
}

/** 次の依頼に添える資料。送信後はその依頼の履歴へ移る。 */
export function useAttachments(projectId: string) {
  const items = ref<Attachment[]>([]), loading = ref(false), error = ref('')

  async function refresh() {
    try { items.value = (await api<{ attachments: Attachment[] }>(
      `/api/projects/${projectId}/attachments`)).attachments ?? [] }
    catch (e) { error.value = e instanceof Error ? e.message : '添付を取得できません。' }
  }

  async function add(file: File): Promise<Attachment | null> {
    loading.value = true; error.value = ''
    try {
      // 名前は非ASCIIを含む。ヘッダーへ入れる処理は共通関数へまとめる。
      const data = await uploadAttachment(projectId, file)
      await refresh()
      return data
    } catch (e) {
      error.value = e instanceof Error ? e.message : '添付できませんでした。'
      return null
    }
    finally { loading.value = false }
  }

  async function remove(name: string) {
    loading.value = true; error.value = ''
    try { await api(`/api/projects/${projectId}/attachments/remove`, 'POST', { name }); await refresh() }
    catch (e) { error.value = e instanceof Error ? e.message : '添付を外せませんでした。' }
    finally { loading.value = false }
  }

  return { items, loading, error, refresh, add, remove }
}
