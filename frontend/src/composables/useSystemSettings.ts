import { ref } from 'vue'
import { api } from './useApi'
import type { GeminiModelCandidate, GeminiProbeResult, SystemGemini, SystemWorkloadIdentity } from '@/types'

export type SystemGeminiForm = Omit<SystemGemini, 'backend' | 'api_key_configured' | 'secrets_available'
  | 'environment' | 'agents_synced'> & { backend: 'gemini_api' | 'vertex' }

/** システム設定。Koyorina 自身が使う Gemini。管理者だけ。
 *  APIキーはサーバーから返らない。入れ替えるときだけ api_key を送る。 */
export function useSystemSettings() {
  const gemini = ref<SystemGemini>(), identity = ref<SystemWorkloadIdentity>(), probe = ref<GeminiProbeResult>()
  const loading = ref(false), saving = ref(false), testing = ref(false)
  const error = ref(''), identityError = ref('')
  const path = '/api/system/gemini'

  async function refresh() {
    loading.value = true; error.value = ''
    try { gemini.value = await api<SystemGemini>(path) }
    catch (e) { error.value = e instanceof Error ? e.message : 'システム設定を取得できません。' }
    finally { loading.value = false }
  }

  async function save(input: SystemGeminiForm, apiKey: string) {
    saving.value = true; error.value = ''
    try {
      gemini.value = await api<SystemGemini>(path, 'PUT', { ...input, ...(apiKey ? { api_key: apiKey } : {}) })
      return true
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'システム設定を保存できません。'
      return false
    } finally { saving.value = false }
  }

  async function test() {
    testing.value = true; error.value = ''; probe.value = undefined
    try { probe.value = await api<GeminiProbeResult>(`${path}/test`, 'POST') }
    catch (e) { error.value = e instanceof Error ? e.message : '接続テストを実行できません。' }
    finally { testing.value = false }
  }

  async function loadIdentity() {
    identityError.value = ''
    try { identity.value = await api<SystemWorkloadIdentity>(`${path}/workload-identity`) }
    catch (e) { identityError.value = e instanceof Error ? e.message : 'クラスタの情報を取得できません。' }
  }

  /** 保存済みの接続先で、選択肢に出すモデルの候補を取る。 */
  async function listModels() {
    return (await api<{ models: GeminiModelCandidate[] }>(`${path}/models`, 'POST')).models
  }

  return { gemini, identity, probe, loading, saving, testing, error, identityError, refresh, save, test, loadIdentity,
    listModels }
}
