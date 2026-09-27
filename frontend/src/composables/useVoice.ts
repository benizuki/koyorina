import { onUnmounted, ref } from 'vue'

const RATE = 16000          // 書き起こしに十分で、送る量が小さい
const MAX_SECONDS = 90      // 長い録音は区切ってもらう。失敗したときの取り直しが重い

/** 話した内容を文字にする。文字を返すだけで、依頼は送らない。 */
export function useVoice() {
  const recording = ref(false), busy = ref(false), error = ref(''), seconds = ref(0)
  let context: AudioContext | undefined
  let stream: MediaStream | undefined
  let chunks: Float32Array[] = []
  let ticker: ReturnType<typeof setInterval> | undefined

  async function start() {
    error.value = ''
    chunks = []
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      context = new AudioContext()
      await context.audioWorklet.addModule('/recorder-worklet.js')
      const source = context.createMediaStreamSource(stream)
      const recorder = new AudioWorkletNode(context, 'recorder')
      recorder.port.onmessage = event => chunks.push(new Float32Array(event.data))
      source.connect(recorder)
      // 音は出さない。出力へ繋ぐと自分の声がそのまま返る。
      recorder.connect(context.destination)
      recording.value = true
      seconds.value = 0
      ticker = setInterval(() => {
        seconds.value += 1
        if (seconds.value >= MAX_SECONDS) stop()
      }, 1000)
    } catch {
      error.value = 'マイクを使えませんでした。ブラウザのマイク許可を確認してください。'
      await release()
    }
  }

  async function release() {
    clearInterval(ticker)
    recording.value = false
    stream?.getTracks().forEach(track => track.stop())
    await context?.close().catch(() => undefined)
    context = undefined
    stream = undefined
  }

  /** 録音を止めて書き起こす。失敗したときは空文字を返す。 */
  async function stop(): Promise<string> {
    if (!recording.value) return ''
    const rate = context?.sampleRate ?? RATE
    await release()
    const wav = encodeWav(downsample(merge(chunks), rate, RATE), RATE)
    chunks = []
    if (wav.size <= 44) { error.value = '音を拾えませんでした。もう一度お試しください。'; return '' }
    busy.value = true
    try {
      const response = await fetch('/api/voice', { method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'audio/wav' }, body: wav })
      const data = await response.json().catch(() => null)
      if (!response.ok || !data) throw new Error(data?.error ?? '書き起こせませんでした。')
      return (data.text as string) ?? ''
    } catch (e) {
      error.value = e instanceof Error ? e.message : '書き起こせませんでした。'
      return ''
    } finally { busy.value = false }
  }

  onUnmounted(release)
  return { recording, busy, error, seconds, start, stop }
}

function merge(chunks: Float32Array[]): Float32Array {
  const total = chunks.reduce((sum, chunk) => sum + chunk.length, 0)
  const merged = new Float32Array(total)
  let offset = 0
  for (const chunk of chunks) { merged.set(chunk, offset); offset += chunk.length }
  return merged
}

/** まとめて平均を取る。間引くだけだと高い音が濁る。 */
function downsample(samples: Float32Array, from: number, to: number): Float32Array {
  if (from <= to) return samples
  const ratio = from / to
  const result = new Float32Array(Math.floor(samples.length / ratio))
  for (let index = 0; index < result.length; index += 1) {
    const start = Math.floor(index * ratio), end = Math.min(Math.floor((index + 1) * ratio), samples.length)
    let sum = 0
    for (let position = start; position < end; position += 1) sum += samples[position]
    result[index] = sum / Math.max(1, end - start)
  }
  return result
}

/** 16bit PCMのWAVにする。ブラウザ既定のwebm/opusは受け手の対応が揃わない。 */
function encodeWav(samples: Float32Array, rate: number): Blob {
  const buffer = new ArrayBuffer(44 + samples.length * 2)
  const view = new DataView(buffer)
  const text = (offset: number, value: string) => {
    for (let index = 0; index < value.length; index += 1) view.setUint8(offset + index, value.charCodeAt(index))
  }
  text(0, 'RIFF'); view.setUint32(4, 36 + samples.length * 2, true); text(8, 'WAVE')
  text(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true)
  view.setUint16(22, 1, true); view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true)
  view.setUint16(32, 2, true); view.setUint16(34, 16, true)
  text(36, 'data'); view.setUint32(40, samples.length * 2, true)
  for (let index = 0; index < samples.length; index += 1) {
    const value = Math.max(-1, Math.min(1, samples[index]))
    view.setInt16(44 + index * 2, value < 0 ? value * 0x8000 : value * 0x7fff, true)
  }
  return new Blob([buffer], { type: 'audio/wav' })
}
