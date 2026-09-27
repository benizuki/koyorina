import { ref } from 'vue'
import { api } from '@/composables/useApi'
import type { NetworkAuditSummary } from '@/types'

export function useNetworkAudit() {
  const summary = ref<NetworkAuditSummary>()
  const loading = ref(false)
  const error = ref('')

  async function refresh(minutes: number, verdict: string) {
    loading.value = true
    error.value = ''
    try {
      const query = new URLSearchParams({ minutes: String(minutes), verdict })
      summary.value = await api<NetworkAuditSummary>(`/api/cluster/network/flows?${query}`)
    } catch (value) {
      error.value = value instanceof Error ? value.message : '通信状況を取得できません。'
    } finally {
      loading.value = false
    }
  }

  return { summary, loading, error, refresh }
}
