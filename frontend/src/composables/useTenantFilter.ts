import { computed, ref, type Ref } from 'vue'
import type { Project, Tenant } from '@/types'

export const ALL = ''
export const UNASSIGNED = 'none'

/**
 * 一覧をテナントで絞る。
 *
 * 所属・管理しているテナントは、アプリがまだなくても選べるようにする。
 * 共有アプリがある所属外テナントも一覧から落とさない。
 *
 * 覚えない（毎回「すべて」から始まる）。絞ったまま次に開くと、
 * 「作ったはずのアプリが無い」と受け取られる。絞っていることが画面に出ていても、
 * 前回の操作は忘れているので結び付かない。
 */
export function useTenantFilter(projects: Ref<Project[]>, tenants: Ref<Tenant[]>, accessibleIds: Ref<Set<string>>) {
  const selected = ref<string>(ALL)

  /** 所属先とアプリがあるテナント。未分類のアプリがあれば、その行も足す。 */
  const choices = computed(() => {
    const used = new Set(projects.value.map(p => p.tenant_id ?? null))
    const named = tenants.value
      .filter(tenant => accessibleIds.value.has(tenant.id) || used.has(tenant.id))
      .map(tenant => ({ value: tenant.id, title: tenant.name }))
    // 名前を引けなかったテナントも落とさない。数が合わないほうが分かりにくい。
    const unknown = [...used].filter(
      (id): id is string => !!id && !tenants.value.some(tenant => tenant.id === id))
    return [
      { value: ALL, title: 'すべてのテナント' },
      ...named,
      ...unknown.map(id => ({ value: id, title: '名称未取得' })),
      ...(used.has(null) ? [{ value: UNASSIGNED, title: '未分類' }] : []),
    ]
  })

  /** 「すべて」以外に選べるテナントがあれば表示する。 */
  const offered = computed(() => choices.value.length > 1)

  function matches(project: Project): boolean {
    if (selected.value === ALL) return true
    if (selected.value === UNASSIGNED) return !project.tenant_id
    return project.tenant_id === selected.value
  }

  const nameOf = (id?: string | null) =>
    tenants.value.find(tenant => tenant.id === id)?.name ?? ''

  return { selected, choices, offered, matches, nameOf }
}
