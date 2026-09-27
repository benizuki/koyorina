// 録音した音の断片を本体へ渡すだけの処理。加工はしない。
// インラインで書けないのはCSPがscript-src 'self'のため。ここは同じ出所のファイル。
class RecorderProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0] && inputs[0][0]
    if (input) this.port.postMessage(input.slice(0))
    return true
  }
}
registerProcessor('recorder', RecorderProcessor)
