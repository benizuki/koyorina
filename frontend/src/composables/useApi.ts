export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message) }
}
export async function api<T>(path: string, method = 'GET', payload?: unknown): Promise<T> {
  const response = await fetch(path, { method, credentials: 'same-origin',
    headers: method === 'GET' ? {} : { 'Content-Type': 'application/json' },
    body: method === 'GET' ? undefined : JSON.stringify(payload ?? {}),
  })
  // サーバーが落ちた・中継で止まった場合はJSONが返らない。内部の解析エラーを見せない。
  const data = await response.json().catch(() => null)
  if (data === null) {
    throw new ApiError(response.ok ? '応答を読み取れませんでした。時間をおいて再度お試しください。'
      : 'サーバーが応答しませんでした。時間をおいて再度お試しください。', response.status)
  }
  if (!response.ok) throw new ApiError(data.error ?? '処理できませんでした。再度お試しください。', response.status)
  return data as T
}
