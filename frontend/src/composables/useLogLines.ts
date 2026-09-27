import { computed } from 'vue'
import type { Ref } from 'vue'

export type LogKind = 'error' | 'warn' | 'ready' | 'info' | 'plain'
export type LogLine = { key: number; text: string; kind: LogKind }

// 同じ濃さで流れる文字列から、危ない行だけを拾うのは読み慣れた人にしかできない。
// 行の種類で色を変え、どこを読めばよいかを形で示す。
const ERROR = /ERROR|CRITICAL|FATAL|Traceback|Exception|[A-Za-z]Error\b|panic|npm ERR!|Errno|OOMKilled/
const WARN = /WARN|Deprecat/i
// 「起動しました」と言えるのは受付を始めた時点。プロセスが立った時点ではない。
const READY = /Application startup complete|Uvicorn running|listening|ready in|起動しました/i
const INFO = /^(INFO|DEBUG)\b|^\[preview\]/
// アクセスログの応答コード。500番台は利用者に「画面が壊れている」として見えている。
const STATUS = /"\s+(\d{3})\b/

function classify(text: string): LogKind {
  const status = STATUS.exec(text)
  if (status) {
    const code = Number(status[1])
    if (code >= 500) return 'error'
    if (code >= 400) return 'warn'
  }
  if (ERROR.test(text)) return 'error'
  if (WARN.test(text)) return 'warn'
  if (READY.test(text)) return 'ready'
  if (INFO.test(text.trimStart())) return 'info'
  return 'plain'
}

export function useLogLines(logs: Ref<string>) {
  return computed<LogLine[]>(() => {
    const text = (logs.value ?? '').replace(/\s+$/, '')
    return text ? lineList(text) : []
  })
}

function lineList(logs: string): LogLine[] {
  return logs.split('\n')
    .reduce<LogLine[]>((lines, raw, key) => {
      const text = raw.replace(/\s+$/, '')
      let kind = classify(text)
      // Tracebackの続きを切り離さない。まとまりで読めないと、どこの話か分からなくなる。
      const previous = lines[lines.length - 1]
      if (kind === 'plain' && previous?.kind === 'error' && /^\s/.test(raw)) kind = 'error'
      lines.push({ key, text, kind })
      return lines
    }, [])
}
