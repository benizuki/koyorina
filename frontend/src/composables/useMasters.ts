import { ref } from 'vue'
import { api } from './useApi'
import type { AiUsageSummary, Department, ManagedUser, Role, RoleCatalog, StorageUsageSummary, Tenant } from '@/types'

/** 利用者と所属組織のマスター。触れるのは管理者だけ。 */
export function useMasters() {
  const users = ref<ManagedUser[]>([]), departments = ref<Department[]>([]), roles = ref<Role[]>([])
  // テナントごとに独立して割り当てるロールの選択肢。
  const tenantRoles = ref<Role[]>([])
  const tenants = ref<Tenant[]>([])
  const usage = ref<AiUsageSummary>()
  const storage = ref<StorageUsageSummary>()
  const loading = ref(false), error = ref('')

  async function perform<T>(work: () => Promise<T>): Promise<T | undefined> {
    loading.value = true; error.value = ''
    try { return await work() }
    catch (e) { error.value = e instanceof Error ? e.message : '処理できませんでした。'; return undefined }
    finally { loading.value = false }
  }

  async function refreshUsage(start?: string, end?: string) {
    const query = start && end ? `?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}` : ''
    await perform(async () => { usage.value = await api<AiUsageSummary>(`/api/ai-usage${query}`) })
  }

  async function refreshStorage(measure = false) {
    await perform(async () => {
      storage.value = await api<StorageUsageSummary>('/api/admin/storage-usage', measure ? 'POST' : 'GET',
                                                     measure ? {} : undefined)
    })
  }

  async function refresh() {
    await perform(async () => {
      const [list, master, kinds, units] = await Promise.all([
        api<ManagedUser[]>('/api/users'), api<Department[]>('/api/departments'),
        api<RoleCatalog>('/api/roles'), api<Tenant[]>('/api/tenants')])
      users.value = list; departments.value = master; tenants.value = units
      roles.value = kinds.system; tenantRoles.value = kinds.tenant
    })
  }

  const saveUser = (user: Partial<ManagedUser>) => perform(async () => {
    const body = { email: user.email, display_name: user.display_name ?? '', role: user.role ?? 'member',
                   department_id: user.department_id ?? null,
                   google_subject: user.google_subject ?? '', enabled: user.enabled ?? true,
                   codex_enabled: user.codex_enabled ?? true,
                   // 所属テナントと、そこでのロール。
                   tenants: user.tenants ?? [] }
    await api(user.id ? `/api/users/${user.id}` : '/api/users', user.id ? 'PATCH' : 'POST', body)
    await refresh()
    return true
  })

  const removeUser = (user: ManagedUser) => perform(async () => {
    await api(`/api/users/${user.id}`, 'DELETE')
    users.value = users.value.filter(item => item.id !== user.id)
    return true
  })

  const saveDepartment = (department: Partial<Department>) => perform(async () => {
    const body = { name: department.name, note: department.note ?? '', enabled: department.enabled ?? true }
    await api(department.id ? `/api/departments/${department.id}` : '/api/departments',
              department.id ? 'PATCH' : 'POST', body)
    await refresh()
    return true
  })

  const removeDepartment = (department: Department) => perform(async () => {
    await api(`/api/departments/${department.id}`, 'DELETE')
    await refresh()
    return true
  })

  const saveTenant = (tenant: Partial<Tenant>) => perform(async () => {
    const body = { name: tenant.name, note: tenant.note ?? '', enabled: tenant.enabled ?? true }
    await api(tenant.id ? `/api/tenants/${tenant.id}` : '/api/tenants',
              tenant.id ? 'PATCH' : 'POST', body)
    await refresh()
    return true
  })

  const removeTenant = (tenant: Tenant) => perform(async () => {
    await api(`/api/tenants/${tenant.id}`, 'DELETE')
    await refresh()
    return true
  })

  return { users, departments, roles, tenantRoles, tenants, usage, storage, loading, error, refresh, refreshUsage,
           refreshStorage,
           saveUser, removeUser, saveDepartment, removeDepartment, saveTenant, removeTenant }
}
