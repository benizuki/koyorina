import { ref } from 'vue'
import { api } from './useApi'
import type { SystemLlm } from '@/types'

export type SystemLlmKind = 'antigravity' | 'openai-compatible' | 'claude'
export type SystemLlmForm = Pick<SystemLlm, 'enabled' | 'model' | 'agent' | 'max_total_tokens' | 'label' | 'base_url'
  | 'backend' | 'gcp_project' | 'location'>

/** システム設定の Antigravity／OpenAI互換API。管理者だけ。
 *  APIキーはサーバーから返らない。入れ替えるときだけ api_key を送る。 */
export function useSystemLlm(kind: SystemLlmKind) {
  const current = ref<SystemLlm>(), loading = ref(false), saving = ref(false), error = ref('')
  const path = `/api/system/llm/${kind}`

  async function refresh() {
    loading.value = true; error.value = ''
    try { current.value = await api<SystemLlm>(path) }
    catch (e) { error.value = e instanceof Error ? e.message : '設定を取得できません。' }
    finally { loading.value = false }
  }

  async function save(input: SystemLlmForm, apiKey: string) {
    saving.value = true; error.value = ''
    try {
      current.value = await api<SystemLlm>(path, 'PUT', { ...input, ...(apiKey ? { api_key: apiKey } : {}) })
      return true
    } catch (e) {
      error.value = e instanceof Error ? e.message : '設定を保存できません。'
      return false
    } finally { saving.value = false }
  }

  return { current, loading, saving, error, refresh, save }
}
