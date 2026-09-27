import { ref } from 'vue'
import { api } from './useApi'
import type { Collaborator, CollaboratorView, Project } from '@/types'

/** アプリを一緒に触る人の出し入れ。削除と共有設定はオーナーと管理者だけが行える。 */
export function useCollaborators(projectId: string) {
  const collaborators = ref<Collaborator[]>([])
  const candidates = ref<Collaborator[]>([])
  const canAdminister = ref(false)
  const loading = ref(false), error = ref('')

  async function perform<T>(action: () => Promise<T>) {
    loading.value = true; error.value = ''
    try { return await action() }
    catch (e) { error.value = e instanceof Error ? e.message : '共同開発者を更新できません。' }
    finally { loading.value = false }
  }
  async function refresh() {
    await perform(async () => {
      const view = await api<CollaboratorView>(`/api/projects/${projectId}/collaborators`)
      collaborators.value = view.collaborators
      canAdminister.value = view.can_administer
      // 候補を引けるのは呼べる人だけ。引けなくても一覧の表示は続ける。
      if (!view.can_administer) { candidates.value = []; return }
      candidates.value = (await api<{ candidates: Collaborator[] }>(
        `/api/projects/${projectId}/collaborators/candidates`)).candidates
    })
  }
  const add = (userId: string) => perform(async () => {
    collaborators.value = (await api<CollaboratorView>(`/api/projects/${projectId}/collaborators`,
      'POST', { user_id: userId })).collaborators
    await refresh()
  })
  const remove = (userId: string) => perform(async () => {
    collaborators.value = (await api<CollaboratorView>(
      `/api/projects/${projectId}/collaborators/${userId}`, 'DELETE')).collaborators
    await refresh()
  })
  // オーナーの引き継ぎ。作業場所はアプリ単位の共有領域にあるので、持ち主の記録だけが移る。
  const handOver = (userId: string) => perform(async () => {
    const project = await api<Project>(`/api/projects/${projectId}/owner`, 'POST', { user_id: userId })
    await refresh()
    return project
  })
  return { collaborators, candidates, canAdminister, loading, error, refresh, add, remove, handOver }
}
