import { ref } from 'vue'
import { api } from './useApi'

/** 動作確認へ出した区切りの履歴。生成1回ごとではない。 */
export interface Deployment {
  at: string; job_id: string; revision: number
  instruction: string | null; generated_at: string
}

export function useDeployments(projectId: string) {
  const deployments = ref<Deployment[]>([]), loading = ref(false), error = ref('')
  async function refresh() {
    loading.value = true; error.value = ''
    try { deployments.value = await api<Deployment[]>(`/api/projects/${projectId}/deployments`) }
    catch (e) { error.value = e instanceof Error ? e.message : '履歴を取得できません。' }
    finally { loading.value = false }
  }
  return { deployments, loading, error, refresh }
}
