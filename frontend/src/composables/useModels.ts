import { ref } from 'vue'
import { api } from './useApi'

/** 選べるモデル。本人のCodex接続から取得する。 */
export interface GeneratorModel {
  id: string; label: string; description: string; provider: 'codex' | 'gemini' | 'antigravity' | 'openai_compatible' | 'claude'
  efforts: string[]; default_effort: string; is_default: boolean
}

const models = ref<GeneratorModel[]>([]), loaded = ref(false)

export function useModels() {
  /** tenantId を渡すと、そのテナントの生成AIの設定（無ければシステムの既定）で選択肢を出す。 */
  async function refresh(tenantId?: string | null) {
    try {
      const query = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ''
      models.value = (await api<{ models: GeneratorModel[] }>(`/api/codex/models${query}`)).models
    } catch { models.value = [] }
    loaded.value = true
  }
  return { models, loaded, refresh }
}
