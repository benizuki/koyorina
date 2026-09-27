import { ref, readonly, computed } from 'vue'
import type { User, Config } from '@/types'
import { api, ApiError } from './useApi'
const user = ref<User | null>(null)
const config = ref<Config | null>(null)
const loaded = ref(false)
const error = ref('')
type GoogleIdentity = { accounts: { id: {
  initialize: (options: { client_id: string; callback: (response: { credential: string }) => void }) => void
  renderButton: (element: HTMLElement, options: Record<string, string>) => void
} } }
declare global { interface Window { google?: GoogleIdentity } }
export function useAuth() {
  async function fetchMe() {
    error.value = ''
    try { config.value = await api<Config>('/api/config'); user.value = await api<User>('/api/me') }
    catch (e) { user.value = null; if (!(e instanceof ApiError && e.status === 401)) error.value = e instanceof Error ? e.message : '接続を確認してください。' }
    finally { loaded.value = true }
  }
  async function mountGoogle(element: HTMLElement) {
    if (!config.value?.google_client_id) return
    if (!window.google) await new Promise<void>((resolve, reject) => {
      const script = document.createElement('script'); script.src = 'https://accounts.google.com/gsi/client'
      script.onload = () => resolve(); script.onerror = () => reject(new Error('Googleログインを読み込めませんでした。'))
      document.head.appendChild(script)
    })
    window.google?.accounts.id.initialize({ client_id: config.value.google_client_id, callback: (response) => {
      void api('/auth/google', 'POST', response).then(fetchMe).catch((e: Error) => { error.value = e.message })
    } })
    window.google?.accounts.id.renderButton(element, { theme: 'outline', size: 'large', locale: 'ja' })
  }
  async function logout() { await api('/auth/logout', 'POST'); user.value = null }
  return { user: readonly(user), config: readonly(config), loaded: readonly(loaded), error,
    authMode: computed(() => config.value?.auth_mode ?? 'google'),
    role: computed(() => user.value?.can_manage_users ? '管理者' : 'メンバー'), fetchMe, mountGoogle, logout }
}
