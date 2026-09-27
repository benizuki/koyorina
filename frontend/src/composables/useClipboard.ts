import { ref } from 'vue'

/** 画面の文面をクリップボードへ写す。押したことが分かるよう、2秒だけ「コピーしました」にする。 */
export function useClipboard() {
  const copied = ref(false), error = ref('')
  let timer: ReturnType<typeof setTimeout> | undefined

  async function copy(text: string) {
    error.value = ''
    try {
      await navigator.clipboard.writeText(text)
      copied.value = true
      clearTimeout(timer)
      timer = setTimeout(() => { copied.value = false }, 2000)
      return true
    } catch {
      error.value = 'コピーできませんでした。ブラウザでクリップボードの利用が許可されているか確認してください。'
      return false
    }
  }
  function reset() { copied.value = false; clearTimeout(timer) }

  return { copied, error, copy, reset }
}
