import { onUnmounted, ref } from 'vue'
import { api } from './useApi'
import type { CodexStatus, DeviceLogin } from '@/types'

export function useCodex() {
  const status = ref<CodexStatus>(), loading = ref(false), error = ref('')
  let timer: ReturnType<typeof setTimeout> | undefined
  let disposed = false
  async function refresh() {
    clearTimeout(timer)
    try { status.value = await api<CodexStatus>('/api/codex/status') }
    catch (e) { error.value = e instanceof Error ? e.message : '接続状態を確認できません。' }
    if (!disposed && requested && status.value?.status === 'disconnected') {
      requested = false
      return login()  // 準備が終わった。押されたログインをここで始める。
    }
    if (!disposed && ['pending', 'preparing'].includes(status.value?.status ?? '')) timer = setTimeout(refresh, 3000)
  }
  // 実行環境の用意が終わったら、押された操作を引き継いで自動で始める。
  let requested = false
  async function login() {
    loading.value = true; error.value = ''
    try {
      const result = await api<DeviceLogin | CodexStatus>('/api/codex/login', 'POST')
      if ('status' in result) { requested = true; status.value = result }
      else status.value = { status: 'pending', email: null, plan: null, login: result, error: null, busy: false }
    } catch (e) { error.value = e instanceof Error ? e.message : 'ログインを開始できません。' }
    finally { loading.value = false; await refresh() }
  }
  async function logout() {
    loading.value = true; error.value = ''
    try { await api('/api/codex/logout', 'POST'); await refresh() }
    catch (e) { error.value = e instanceof Error ? e.message : '接続を解除できません。' }
    finally { loading.value = false }
  }
  onUnmounted(() => { disposed = true; clearTimeout(timer) })
  return { status, loading, error, refresh, login, logout }
}
