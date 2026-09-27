import { computed, ref, type Ref } from 'vue'
import type { Project, Tenant } from '@/types'

export const ALL = ''
export const UNASSIGNED = 'none'

/**
 * 一覧をテナントで絞る。
 *
 * 選べるのは「実際にアプリがあるテナント」だけにする。全テナントを並べると、
 * 選んでも必ず空になる選択肢が混ざり、絞り込みが効いていないように見える。
 *
 * 覚えない（毎回「すべて」から始まる）。絞ったまま次に開くと、
 * 「作ったはずのアプリが無い」と受け取られる。絞っていることが画面に出ていても、
 * 前回の操作は忘れているので結び付かない。
 */
export function useTenantFilter(projects: Ref<Project[]>, tenants: Ref<Tenant[]>) {
  const selected = ref<string>(ALL)

  /** 実際にアプリがあるテナントだけ。未分類のアプリがあれば、その行も足す。 */
  const choices = computed(() => {
    const used = new Set(projects.value.map(p => p.tenant_id ?? null))
    const named = tenants.value
      .filter(tenant => used.has(tenant.id))
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

  /** 選択肢が1つしか無いなら出さない。押しても何も変わらないものを並べない。 */
  const offered = computed(() => choices.value.length > 2)

  function matches(project: Project): boolean {
    if (selected.value === ALL) return true
    if (selected.value === UNASSIGNED) return !project.tenant_id
    return project.tenant_id === selected.value
  }

  const nameOf = (id?: string | null) =>
    tenants.value.find(tenant => tenant.id === id)?.name ?? ''

  return { selected, choices, offered, matches, nameOf }
}
