import { ref } from 'vue'
import { api } from './useApi'
import type { GeminiProbeResult, TenantAiSettings, WorkloadIdentityInfo } from '@/types'

/** テナントの生成アプリが使うGemini。管理者だけが扱う。
 *
 *  APIキーはサーバーから返らない。入れ替えるときだけ api_key を送り、
 *  送らなければ前の値が残る。
 */
export function useTenantAi(tenantId: string) {
  const settings = ref<TenantAiSettings>(), identity = ref<WorkloadIdentityInfo>()
  const loading = ref(false), saving = ref(false), error = ref(''), identityError = ref('')
  const testing = ref(false), probe = ref<GeminiProbeResult>()
  const path = `/api/tenants/${tenantId}/ai-settings`

  async function refresh() {
    loading.value = true; error.value = ''
    try { settings.value = await api<TenantAiSettings>(path) }
    catch (e) { error.value = e instanceof Error ? e.message : 'Geminiの設定を取得できません。' }
    finally { loading.value = false }
  }

  async function save(input: Omit<TenantAiSettings, 'api_key_configured' | 'secrets_available'>, apiKey: string) {
    saving.value = true; error.value = ''
    try {
      settings.value = await api<TenantAiSettings>(path, 'PUT', { ...input, ...(apiKey ? { api_key: apiKey } : {}) })
      return true
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Geminiの設定を保存できません。'
      return false
    } finally { saving.value = false }
  }

  /** 保存済みの設定で、生成アプリと同じ経路からGeminiへ1回だけ問い合わせる。 */
  async function test() {
    testing.value = true; error.value = ''; probe.value = undefined
    try { probe.value = await api<GeminiProbeResult>(`${path}/test`, 'POST') }
    catch (e) { error.value = e instanceof Error ? e.message : '接続テストを実行できません。' }
    finally { testing.value = false }
  }

  async function loadIdentity() {
    identityError.value = ''
    try { identity.value = await api<WorkloadIdentityInfo>(`${path}/workload-identity`) }
    catch (e) { identityError.value = e instanceof Error ? e.message : 'クラスタの情報を取得できません。' }
  }

  return { settings, identity, loading, saving, testing, probe, error, identityError,
    refresh, save, test, loadIdentity }
}
