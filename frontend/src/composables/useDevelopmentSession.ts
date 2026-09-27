import { onUnmounted, ref } from 'vue'
import { api } from './useApi'
import type { DevelopmentSession } from '@/types'

/**
 * 開発の席。アプリごとに1人だけが編集でき、後から入った人は閲覧のみになる。
 *
 * 期限は実行環境側が持つ（2分）。心拍で延ばし、画面を閉じたら明示的に空ける。
 * 閉じ損ねても期限で空くので、席が永久に埋まったままにはならない。
 */
export function useDevelopmentSession(projectId: string) {
  const session = ref<DevelopmentSession>()
  const error = ref('')
  let timer: ReturnType<typeof setTimeout> | undefined
  let disposed = false

  async function beat(takeover = false) {
    clearTimeout(timer)
    try {
      const query = takeover ? '?takeover=true' : ''
      session.value = await api<DevelopmentSession>(
        `/api/projects/${projectId}/session${query}`, 'POST')
      error.value = ''
    } catch (e) {
      // 席が取れなくても画面は使える。編集できるかは編集時に実行環境が判断する。
      error.value = e instanceof Error ? e.message : '開発中の利用者を確認できません。'
    }
    // 期限の半分より短く打つ。1回取りこぼしても席は空かない。
    if (!disposed) timer = setTimeout(() => beat(), 45_000)
  }
  async function release() {
    clearTimeout(timer)
    try { session.value = await api<DevelopmentSession>(`/api/projects/${projectId}/session`, 'DELETE') }
    catch { /* 閉じるときの失敗は放置してよい。期限で空く。 */ }
  }
  // 画面を離れたら返す。タブごと閉じられた場合は届かないが、期限で空く。
  onUnmounted(() => {
    disposed = true
    clearTimeout(timer)
    void release()
  })
  return { session, error, beat, release, takeOver: () => beat(true) }
}
