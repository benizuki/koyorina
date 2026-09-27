import { onUnmounted, ref, type Ref } from 'vue'
import { api } from './useApi'
import type { Project, RequirementsInterview } from '@/types'

export function useRequirementsInterview(projectId: string, provider: Ref<'codex' | 'gemini'>) {
  const state = ref<RequirementsInterview>({ status: 'idle' })
  const loading = ref(false), error = ref('')
  let timer: number | undefined
  const endpoint = (suffix = '') => `/api/projects/${projectId}/interview${suffix}?provider=${provider.value}`

  function schedule() {
    window.clearTimeout(timer)
    if (['starting', 'thinking', 'waiting'].includes(state.value.status))
      timer = window.setTimeout(() => refresh(), 1800)
  }
  async function run<T>(action: () => Promise<T>) {
    loading.value = true; error.value = ''
    try { return await action() }
    catch (e) { error.value = e instanceof Error ? e.message : '要件ヒアリングへ接続できません。' }
    finally { loading.value = false }
  }
  async function refresh() {
    try { state.value = await api<RequirementsInterview>(endpoint()) }
    catch (e) { error.value = e instanceof Error ? e.message : '要件ヒアリングへ接続できません。' }
    schedule()
  }
  async function start() {
    await run(async () => { state.value = await api(endpoint(), 'POST'); schedule() })
  }
  async function answer(answers: Record<string, string[]>) {
    await run(async () => { state.value = await api(endpoint('/answer'), 'POST', { answers }); schedule() })
  }
  async function cancel() {
    await run(async () => { state.value = await api(endpoint('/cancel'), 'POST', {}) })
  }
  async function apply(revision: number) {
    return run(() => api<Project>(endpoint('/apply'), 'POST', { revision }))
  }
  onUnmounted(() => window.clearTimeout(timer))
  return { state, loading, error, refresh, start, answer, cancel, apply }
}
