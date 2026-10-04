import { ref } from 'vue'
import { api } from './useApi'
import type { AppPublication, BuildDockerfile } from '@/types'

export function usePublication(projectId: string) {
  const base = `/api/projects/${projectId}`
  const state = ref<AppPublication>()
  const error = ref(''), busy = ref(false), logs = ref('')
  const detailBuildId = ref<string>(), dockerfile = ref<BuildDockerfile>()
  const logsBusy = ref(false), dockerfileBusy = ref(false), logsError = ref(''), dockerfileError = ref('')
  let detailVersion = 0
  async function readLogs() {
    const id = detailBuildId.value, version = detailVersion
    if (!id || logsBusy.value) return
    logsBusy.value = true; logsError.value = ''
    try {
      const result = await api<{ logs: string }>(`${base}/builds/${id}/logs`)
      if (version === detailVersion) logs.value = result.logs
    } catch (e) {
      if (version === detailVersion) logsError.value = e instanceof Error ? e.message : 'ログを取得できません。'
    } finally { if (version === detailVersion) logsBusy.value = false }
  }
  async function readDockerfile() {
    const id = detailBuildId.value, version = detailVersion
    dockerfileBusy.value = true; dockerfileError.value = ''
    try {
      const result = await api<BuildDockerfile>(id ? `${base}/builds/${id}/dockerfile` : `${base}/publication/dockerfile`)
      if (version === detailVersion) dockerfile.value = result
    } catch (e) {
      if (version === detailVersion) dockerfileError.value = e instanceof Error ? e.message : 'Dockerfileを取得できません。'
    } finally { if (version === detailVersion) dockerfileBusy.value = false }
  }
  async function selectDetails(id?: string) {
    detailVersion++; detailBuildId.value = id
    logs.value = ''; dockerfile.value = undefined; logsBusy.value = false
    logsError.value = ''; dockerfileError.value = ''
    await Promise.all([readLogs(), readDockerfile()])
  }
  async function refresh() {
    try { state.value = await api<AppPublication>(`${base}/publication`) }
    catch (e) { error.value = e instanceof Error ? e.message : '公開状態を取得できません。' }
  }
  async function perform(action: () => Promise<unknown>) {
    busy.value = true; error.value = ''
    try { await action(); await refresh() }
    catch (e) { error.value = e instanceof Error ? e.message : '操作を完了できません。' }
    finally { busy.value = false }
  }
  return { state, error, busy, logs, detailBuildId, dockerfile,
    logsBusy, dockerfileBusy, logsError, dockerfileError, selectDetails, readLogs, readDockerfile, refresh, perform,
    build: (generationId: string) => perform(() => api(`${base}/builds`, 'POST', { generation_id: generationId })),
    release: (buildId: string) => perform(() => api(`${base}/publication/release`, 'POST', { build_id: buildId })),
    cancel: (buildId: string) => perform(() => api(`${base}/builds/${buildId}/cancel`, 'POST')) }
}
