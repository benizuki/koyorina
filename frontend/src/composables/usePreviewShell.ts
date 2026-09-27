import { ref } from 'vue'
import { api } from './useApi'

export interface CommandResult {
  command: string; stdout: string; stderr: string; exit_code: number | null
}

/**
 * プレビューの中でコマンドを1回だけ実行する。対話シェルではない。
 *
 * 普段は施錠しておく。調べるつもりのない人が、入力欄を見つけて何となく打つ、
 * という入り方を防ぐため。解錠は画面を離れると戻る（保存しない）。
 */
export function usePreviewShell(projectId: string) {
  const unlocked = ref(false)
  const result = ref<CommandResult>()
  const running = ref(false)
  const error = ref('')

  function unlock(): void { unlocked.value = true }
  function lock(): void { unlocked.value = false; error.value = '' }

  async function run(command: string): Promise<boolean> {
    const text = command.trim()
    if (!unlocked.value || !text || running.value) return false
    running.value = true
    error.value = ''
    result.value = undefined
    try {
      const response = await api<CommandResult>(
        `/api/projects/${projectId}/preview/exec`, 'POST', { command: text })
      // 調査は直前の結果だけ分かればよい。履歴を積むと画面が縦へ伸び続ける。
      result.value = response
      return true
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'コマンドを実行できませんでした。'
      return false
    } finally { running.value = false }
  }

  return { unlocked, result, running, error, unlock, lock, run }
}
