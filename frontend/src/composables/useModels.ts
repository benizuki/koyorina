import { ref } from 'vue'
import { api } from './useApi'

/** 選べるモデル。本人のCodex接続から取得する。 */
export interface GeneratorModel {
  id: string; label: string; description: string; provider: 'codex' | 'gemini' | 'antigravity' | 'openai_compatible' | 'claude'
  efforts: string[]; default_effort: string; is_default: boolean
}

/** Codexの選択肢の状態。preparing は本人用の実行環境が起動中で、数十秒で選べるようになる。 */
export type CodexStatus = 'ready' | 'preparing' | 'disconnected' | 'unavailable' | null

const models = ref<GeneratorModel[]>([]), loaded = ref(false), codexStatus = ref<CodexStatus>(null)
// 起動を待って取り直す間隔と回数（約2分）。それでも準備中なら、次に画面を開いたときに取り直す。
const RETRY_MS = 5000, RETRIES = 24
let retry: ReturnType<typeof setTimeout> | undefined

export function useModels() {
  /** tenantId を渡すと、そのテナントの生成AIの設定（無ければシステムの既定）で選択肢を出す。 */
  async function refresh(tenantId?: string | null, attempt = 0) {
    clearTimeout(retry)
    try {
      const query = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ''
      const result = await api<{ models: GeneratorModel[], codex_status?: CodexStatus }>(`/api/codex/models${query}`)
      models.value = result.models
      codexStatus.value = result.codex_status ?? null
    } catch { models.value = []; codexStatus.value = null }
    loaded.value = true
    if (codexStatus.value === 'preparing' && attempt < RETRIES) {
      retry = setTimeout(() => refresh(tenantId, attempt + 1), RETRY_MS)
    }
  }
  return { models, loaded, codexStatus, refresh }
}
