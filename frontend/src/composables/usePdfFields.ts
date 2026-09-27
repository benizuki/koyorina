import { ref } from 'vue'
import type { ExtractedFields } from '@/types'

export function usePdfFields() {
  const busy = ref(false), error = ref('')
  async function encode(file: File): Promise<string> {
    return await new Promise((resolve, reject) => {
      const reader = new FileReader()
      reader.onerror = () => reject(new Error(`${file.name}を読み取れませんでした。`))
      reader.onload = () => resolve(String(reader.result).split(',', 2)[1] ?? '')
      reader.readAsDataURL(file)
    })
  }

  async function extract(files: File[]): Promise<ExtractedFields | undefined> {
    error.value = ''
    if (busy.value) return
    if (!files.length || files.length > 5) {
      error.value = 'PDFは5件まで選べます。'; return
    }
    if (files.some(file => file.size > 5 * 1024 * 1024 || !file.name.toLowerCase().endsWith('.pdf'))) {
      error.value = '1件あたり5MiB以下のPDFを選んでください。'; return
    }
    if (files.reduce((total, file) => total + file.size, 0) > 15 * 1024 * 1024) {
      error.value = 'PDFの合計は15MiB以下にしてください。'; return
    }
    busy.value = true
    try {
      const payload = { files: await Promise.all(files.map(async file => (
        { name: file.name, content: await encode(file) }
      ))) }
      const response = await fetch('/api/pdf-fields/batch', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-PDF-Consent': 'yes' },
        body: JSON.stringify(payload), signal: AbortSignal.timeout(100000),
      })
      if (!response.headers.get('content-type')?.includes('application/json'))
        throw new Error('PDFを送信できませんでした。件数・容量や接続状態を確認してください。')
      const data = await response.json()
      if (!response.ok) throw new Error(data.error ?? '抽出できませんでした。')
      return data as ExtractedFields
    } catch (e) {
      error.value = e instanceof Error && e.name !== 'TimeoutError' ? e.message : '抽出が時間切れになりました。しばらく待ってからお試しください。'
    } finally { busy.value = false }
  }
  return { busy, error, extract }
}
