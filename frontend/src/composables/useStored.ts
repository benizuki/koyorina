import { ref, watch, type Ref } from 'vue'

const PREFIX = 'koyorina.'

// 既定値から型を決めるが、'medium' のような一点の型に狭めない。後で別の値を入れるため。
type Widened<T> = T extends string ? string : number

/** ブラウザに覚えさせる値。読めない環境（プライベートモードなど）では既定で動く。 */
export function stored<T extends string | number>(key: string, fallback: T): Ref<Widened<T>> {
  const state = ref(read(key, fallback)) as Ref<Widened<T>>
  watch(state, value => {
    try { localStorage.setItem(PREFIX + key, String(value)) } catch { /* 保存できなくても動く */ }
  })
  return state
}

function read<T extends string | number>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(PREFIX + key)
    if (raw === null) return fallback
    if (typeof fallback === 'number') {
      const value = Number(raw)
      return (Number.isFinite(value) ? value : fallback) as T
    }
    return raw as T
  } catch {
    return fallback
  }
}
