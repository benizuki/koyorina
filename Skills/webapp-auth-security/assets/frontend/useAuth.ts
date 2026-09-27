import { ref, readonly } from 'vue'
import type { User, Role, Permissions, MeResponse } from '@/types'

// モジュールスコープに置くことで、useAuth() をどこから何度呼んでも
// 同じ状態を指す。ログイン情報は画面ごとに別々だと困るため。
const user = ref<User | null>(null)
const role = ref<Role>('general')
const permissions = ref<Permissions>({
  can_view:         false,
  can_edit:         false,
  can_manage_users: false,
})
const loaded = ref(false)

export function useAuth() {
  async function fetchMe(): Promise<void> {
    try {
      const res = await fetch('/api/me')
      // 401 はセッション切れ。SPA 内で握らず、素直にログイン画面へ返す。
      if (res.status === 401) {
        window.location.href = '/login'
        return
      }
      const data = await res.json() as MeResponse
      user.value        = data.user
      role.value        = data.role
      permissions.value = data.permissions
    } catch {
      window.location.href = '/login'
    } finally {
      loaded.value = true
    }
  }

  // readonly で返し、書き換えは必ずこの composable を経由させる。
  return {
    user:        readonly(user),
    role:        readonly(role),
    permissions: readonly(permissions),
    loaded:      readonly(loaded),
    fetchMe,
  }
}
