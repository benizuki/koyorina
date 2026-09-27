import { computed, ref, type Ref } from 'vue'
import { api } from './useApi'
import type { SystemGemini, SystemLlm, SystemWorkloadIdentity, TenantLlmMode, TenantLlmSettings,
  TenantLlmState } from '@/types'
import type { SystemGeminiForm } from './useSystemSettings'
import type { SystemLlmForm, SystemLlmKind } from './useSystemLlm'

type Kind = keyof TenantLlmSettings

/** テナントの生成AI。システム管理者と、そのテナントのテナント管理者だけ。
 *  APIキーはサーバーから返らない。入れ替えるときだけ api_key を送る。
 *  画面の部品（SystemGemini・SystemLlmProvider）はシステム設定と共通で、下の adapter で差し替える。 */
export function useTenantLlm(tenantId: string) {
  const state = ref<TenantLlmSettings>(), identity = ref<SystemWorkloadIdentity>()
  const loading = ref(false), saving = ref(false), error = ref(''), identityError = ref('')
  const path = `/api/tenants/${tenantId}/llm`

  async function refresh() {
    loading.value = true; error.value = ''
    try { state.value = await api<TenantLlmSettings>(path) }
    catch (e) { error.value = e instanceof Error ? e.message : '設定を取得できません。' }
    finally { loading.value = false }
  }

  async function save(kind: Kind, mode: TenantLlmMode, settings?: object, apiKey = '') {
    saving.value = true; error.value = ''
    try {
      const body = { mode, ...(mode === 'tenant' ? { settings: { ...settings, ...(apiKey ? { api_key: apiKey } : {}) } } : {}) }
      const result = await api<TenantLlmState<SystemGemini & SystemLlm>>(`${path}/${kind}`, 'PUT', body)
      if (state.value) (state.value as Record<Kind, unknown>)[kind] = result
      return true
    } catch (e) {
      error.value = e instanceof Error ? e.message : '設定を保存できません。'
      return false
    } finally { saving.value = false }
  }

  async function loadIdentity() {
    identityError.value = ''
    try {
      // テナントの WIF で名乗るのは生成エージェントだけ（本体はシステム設定の Gemini を使う）。
      const info = await api<{ agent_subject: string; issuer: string; jwks: unknown }>(`${path}/workload-identity`)
      identity.value = { app_subject: '', ...info }
    } catch (e) { identityError.value = e instanceof Error ? e.message : 'クラスタの情報を取得できません。' }
  }

  return { state, identity, loading, saving, error, identityError, refresh, save, loadIdentity }
}

export type TenantLlm = ReturnType<typeof useTenantLlm>

/** 表示に使う値。テナントの設定があればそれ、無ければ比べるためのシステムの既定。 */
function shown<T>(entry: TenantLlmState<T> | undefined): T | undefined {
  return entry ? (entry.settings ?? entry.system) : undefined
}

/** Gemini の部品（SystemGemini.vue）から、システム設定と同じ形で使えるようにする。 */
export function tenantGemini(tenant: TenantLlm) {
  return {
    gemini: computed(() => shown(tenant.state.value?.gemini)) as Ref<SystemGemini | undefined>,
    identity: tenant.identity, loading: tenant.loading, saving: tenant.saving, error: tenant.error,
    identityError: tenant.identityError, probe: ref(), testing: ref(false),
    refresh: tenant.refresh, loadIdentity: tenant.loadIdentity,
    save: (form: SystemGeminiForm, apiKey: string) => tenant.save('gemini', 'tenant', form, apiKey),
    test: async () => undefined,
  }
}

/** Antigravity／OpenAI互換の部品（SystemLlmProvider.vue）から使えるようにする。 */
export function tenantProvider(tenant: TenantLlm, kind: SystemLlmKind) {
  const key: Kind = kind === 'openai-compatible' ? 'openai_compatible' : kind
  return {
    current: computed(() => shown(tenant.state.value?.[key])) as Ref<SystemLlm | undefined>,
    loading: tenant.loading, saving: tenant.saving, error: tenant.error, refresh: tenant.refresh,
    save: (input: SystemLlmForm, apiKey: string) => tenant.save(key, 'tenant', input, apiKey),
  }
}
