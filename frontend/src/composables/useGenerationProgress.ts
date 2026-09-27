import { onMounted, onUnmounted, ref, watch, type Ref } from 'vue'
import { api } from './useApi'

export interface GenerationProgress {
  events: {
    id: number; at: string; kind: 'status' | 'codex' | 'activity' | 'plan' | 'file' | 'command' | 'error'
    message: string; key?: string | null; state?: 'running' | 'done' | 'failed' | null
  }[]
  last_response_at: string | null
  response_bytes: number
  truncated: boolean
}

export function useGenerationProgress(projectId: string, jobId: string, active: Ref<boolean>) {
  const progress = ref<GenerationProgress>({ events: [], last_response_at: null, response_bytes: 0, truncated: false })
  const error = ref(''), refreshing = ref(false)
  let timer: ReturnType<typeof setTimeout> | undefined
  let disposed = false
  async function refresh() {
    if (disposed || refreshing.value) return
    clearTimeout(timer)
    refreshing.value = true
    try {
      const result = await api<GenerationProgress>(`/api/projects/${projectId}/jobs/${jobId}/progress`)
      if (!disposed) { progress.value = result; error.value = '' }
    } catch { if (!disposed) error.value = '作業報告を取得できません。生成の失敗とは限りません。再取得してください。' }
    finally {
      refreshing.value = false
      if (!disposed && active.value) timer = setTimeout(refresh, error.value ? 5000 : 2000)
    }
  }
  onMounted(refresh)
  watch(active, refresh)
  onUnmounted(() => { disposed = true; clearTimeout(timer) })
  return { progress, error, refreshing, refresh }
}
