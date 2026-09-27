# 実装レシピ

SKILL.md の基本形で足りない場面のパターン集。必要なものだけ読めばよい。

## 目次

- [フォームの検証](#フォームの検証)
- [確認ダイアログ](#確認ダイアログ)
- [長時間処理の進捗表示](#長時間処理の進捗表示)
- [Markdown を安全に描画する](#markdown-を安全に描画する)
- [一覧の絞り込みと並べ替え](#一覧の絞り込みと並べ替え)
- [ファイルのダウンロード](#ファイルのダウンロード)

## フォームの検証

Vuetify の `rules` を使う。検証関数は「OK なら `true`、NG ならエラー文字列」を返す。

```vue
<template>
  <v-form v-model="valid" @submit.prevent="submit">
    <v-text-field
      v-model="email"
      label="メールアドレス"
      :rules="[required, emailFormat]"
      density="comfortable"
    />
    <v-btn type="submit" color="primary" :disabled="!valid" :loading="saving">保存</v-btn>
  </v-form>
</template>

<script setup lang="ts">
import { ref } from 'vue'

const valid  = ref(false)
const saving = ref(false)
const email  = ref('')

const required = (v: string) => !!v?.trim() || '入力してください'
const emailFormat = (v: string) =>
  /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v) || 'メールアドレスの形式で入力してください'
</script>
```

検証関数はコンポーネントの外（`src/utils/rules.ts` など）に出して使い回す。
同じ検証が画面ごとに微妙に違うと、片方だけ通ってしまう入力が生まれる。

**フロントの検証は入力を助けるためのもので、防御ではない。**
同じ検証をバックエンドにも必ず書く。

## 確認ダイアログ

削除のような取り消せない操作は、必ず一段挟む。

```vue
<template>
  <v-dialog v-model="open" max-width="420">
    <v-card>
      <v-card-title class="pa-4 pb-2">削除の確認</v-card-title>
      <v-card-text class="pa-4 pt-2">
        {{ target?.name }} を削除します。この操作は取り消せません。
      </v-card-text>
      <v-card-actions class="pa-4 pt-0">
        <v-spacer />
        <v-btn variant="text" @click="open = false">キャンセル</v-btn>
        <v-btn color="error" variant="flat" :loading="busy" @click="confirm">削除する</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>
```

「この操作は取り消せません」を本文に書く。ボタンのラベルは「はい」ではなく
「削除する」にする。押す直前に何が起きるかが読めるようにするため。

## 長時間処理の進捗表示

LLM 呼び出しや大きな集計のように数十秒かかる処理は、
**リクエスト ID を先に返して、フロントが進捗をポーリングする**形にする。
1 本の HTTP を長時間開いたままにすると、ロードバランサやプロキシに切られる。

```ts
export function useLongTask() {
  const progress = ref<TaskProgress | null>(null)
  const running  = ref(false)

  async function start(payload: unknown): Promise<TaskResult> {
    running.value = true
    // 進捗を突き合わせる ID はフロントで作って渡す。
    // サーバの応答を待たずにポーリングを始められる。
    const requestId = crypto.randomUUID()
    const timer = window.setInterval(() => void poll(requestId), 1500)
    try {
      const res = await fetch('/api/tasks', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ ...payload as object, request_id: requestId }),
      })
      const data = await res.json() as TaskResult & { error?: string }
      if (!res.ok) throw new Error(data.error ?? `HTTP ${res.status}`)
      return data
    } finally {
      window.clearInterval(timer)
      running.value  = false
      progress.value = null
    }
  }

  async function poll(requestId: string): Promise<void> {
    try {
      const res = await fetch(`/api/progress/${requestId}`)
      if (res.ok) progress.value = await res.json() as TaskProgress
    } catch {
      // 進捗の取得失敗で本処理を止める必要はない。次の周期で拾い直す。
    }
  }

  return { progress, running, start }
}
```

進捗表示には「今どの段階か」を日本語で出す。パーセンテージだけだと、
止まっているのか進んでいるのか分からず、利用者が再実行してしまう。

```vue
<v-card v-if="running" variant="tonal" class="mb-4">
  <v-card-text class="d-flex align-center ga-3">
    <v-progress-circular indeterminate size="20" width="2" color="primary" />
    <span>{{ progress?.message ?? '処理を開始しています' }}</span>
  </v-card-text>
</v-card>
```

## Markdown を安全に描画する

`marked` で HTML にしてから `v-html` で出す（同梱の `package.json` には入れていないので `npm i marked` する）。ただし **`v-html` は XSS の入口**なので、
入れてよいのは自分のバックエンドが生成した文字列だけ。
利用者の入力や外部サービスの応答をそのまま流すなら、DOMPurify を挟む。

```ts
import { marked } from 'marked'

const html = computed(() => marked.parse(props.text, { async: false }) as string)
```

```vue
<div class="markdown-body" v-html="html" />
```

LLM の出力を描画する場合は、たとえ自前のバックエンド経由でも
利用者や外部データの内容が混ざりうる。この場合は必ずサニタイズすること。

## 一覧の絞り込みと並べ替え

`v-data-table` は `search` と `sort-by` を持っているので、自前で実装しない。

```vue
<v-text-field
  v-model="search"
  label="検索"
  prepend-inner-icon="mdi-magnify"
  density="compact"
  clearable
  hide-details
  class="mb-3"
/>
<v-data-table :headers="headers" :items="items" :search="search" />
```

件数が数千件を超えると、クライアント側の絞り込みでは重くなる。
その段階でサーバ側のページングに切り替える（`v-data-table-server` を使う）。

## ファイルのダウンロード

バックエンドが生成したファイルは、`fetch` で Blob として受けてから保存する。
`window.open` だと Cookie が付かない場合や、エラー時に真っ白な画面が出る場合がある。

```ts
async function download(url: string, filename: string): Promise<void> {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  const blob = await res.blob()
  const objectUrl = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = objectUrl
  a.download = filename
  a.click()
  URL.revokeObjectURL(objectUrl)
}
```
