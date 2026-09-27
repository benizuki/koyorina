import { ref } from 'vue'
import type { Project, ProjectInput } from '@/types'
import { api } from './useApi'
export function useProjects() {
  const projects = ref<Project[]>([]), loading = ref(false), error = ref('')
  async function perform<T>(action: () => Promise<T>): Promise<T | undefined> {
    if (loading.value) return
    loading.value = true; error.value = ''
    try { return await action() } catch (e) { error.value = e instanceof Error ? e.message : '接続を確認してください。' }
    finally { loading.value = false }
  }
  async function refresh() { projects.value = await api<Project[]>('/api/projects') }
  async function save(input: ProjectInput, existing?: Project, tenantId?: string) {
    return perform(async () => {
      // テナントは仕様ではなく置き場の区分。新規作成のときだけ、問い合わせで渡す。
      const query = !existing && tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : ''
      const project = await api<Project>(
        existing ? `/api/projects/${existing.id}` : `/api/projects${query}`,
        existing ? 'PUT' : 'POST', existing ? { ...input, revision: existing.revision } : input)
      await refresh(); return project
    })
  }
  async function approve(project: Project) {
    return perform(async () => {
      const result = await api<Project>(`/api/projects/${project.id}/approve`, 'POST', { revision: project.revision })
      await refresh(); return result
    })
  }
  async function remove(project: Project) {
    return perform(async () => {
      const result = await api<{ deleted: boolean; remaining: string[] }>(`/api/projects/${project.id}`, 'DELETE')
      await refresh(); return result
    })
  }
  return { projects, loading, error, perform, refresh, save, approve, remove }
}
