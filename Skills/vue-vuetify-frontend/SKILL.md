---
name: vue-vuetify-frontend
description: TypeScript + Vue 3 (Composition API / script setup) + Vuetify 3 + Vite でフロントエンドを書くためのスキル。画面・コンポーネント・ダイアログ・フォーム・一覧表を新しく作る、composable で状態や API 呼び出しを整理する、配色・文字サイズ・余白・チップなど見た目を揃える、ライト／ダークテーマを切り替える、Vuetify のテーマやデザイントークンを決める、vue-tsc の型エラーを直す、といったときに必ず参照すること。「管理画面を作って」「一覧に検索を付けて」「ダイアログを出したい」「フロントを直して」「見た目がばらついている」「色や文字サイズを揃えたい」「ダークモードに対応して」のように Vue や Vuetify という単語が出ていなくても、この構成のフロントエンドを触るなら使う。プロジェクト全体の構成は webapp-scaffold、ログイン状態と権限の扱いは webapp-auth-security を参照する。
---

# Vue 3 + Vuetify フロントエンド

非エンジニアが日常業務で使う社内向け画面を、少ない記述で一定の品質に揃えるための構成。

## 前提と方針

- **Composition API + `<script setup lang="ts">` のみ。** Options API は混ぜない
- **状態管理ライブラリは入れない。** Pinia を入れるほどの規模になることは稀で、
  composable の中に `ref` を置くだけで足りる。足りなくなったら Pinia を検討する
- **`tsconfig.json` は `strict: true`。** `any` を書くくらいなら型を定義する。
  バックエンドのレスポンス型は `src/types.ts` に集約し、そこだけ見れば API の形が分かるようにする
- **Vuetify のコンポーネントを素直に使う。** 独自の CSS で見た目を作り込むより、
  `v-card` / `v-data-table` / `v-dialog` の既定の見た目に寄せるほうが、
  後から触る人が壊さずに済む
- **見た目の値はトークンにする。** 色・文字サイズ・余白・角丸は
  `src/styles/tokens.css` にしか書かない。コンポーネントに生の `px` と `hex` を
  書き始めると、画面が増えるほど不揃いが積み上がり、直すときは全画面の修正になる

## 仕様書から書き起こす

`docs/app-spec.md`（`webapp-scaffold` のヒアリングで作る）があるなら、
エンティティの項目表が `types.ts` とフォームにそのまま写る。

| 仕様書の型 | `types.ts` | 入力部品 | 一覧での見せ方 |
|---|---|---|---|
| `text` / `longtext` | `string` | `v-text-field` / `v-textarea` | そのまま／省略表示 |
| `number` / `money` | `number` | `v-text-field type="number"` | `.numeric`（右寄せ） |
| `date` / `datetime` | `string` | `v-text-field type="date"` | そのまま |
| `bool` | `boolean` | `v-checkbox` | `.chip` |
| `enum(A,B,C)` | `'A' \| 'B' \| 'C'` | `v-select` | **`.chip` で色分け** |
| `ref(entity)` / `user` | `string` | `v-autocomplete` | 参照先の表示名 |
| `file` | `string[]` | `v-file-input` | 件数のみ |

- 仕様書の `一覧` に ✓ が付いた項目だけを `v-data-table` の列にする（5〜7 列まで）
- `検索` に ✓ が付いた項目が絞り込みの軸になる
- `enum` のチップ色は仕様書の備考にある（`.chip--brand` / `--warn` / `--danger`）
- 項目が 10 個以下ならダイアログ、それ以上なら画面にする

## 立ち上げ

`assets/` をプロジェクトの `frontend/` にコピーし、`__APP_TITLE__` を置換する。

```bash
SKILL_DIR="${APP_FORGE_SKILLS:-$HOME/.claude/skills}/vue-vuetify-frontend"
mkdir -p frontend && cp -R "$SKILL_DIR/assets/." frontend/
cd frontend && npm install
```

同梱しているもの:

| ファイル | 中身 |
|---|---|
| `package.json` | Vue 3 / Vuetify 3 / Vite / vue-tsc。`build` は型チェック込み |
| `tsconfig.json` | strict、`@/*` → `src/*` のパスエイリアス |
| `vite.config.ts` | Vuetify の autoImport、`/api` などのプロキシ、`dist` の出力先 |
| `src/styles/tokens.css` | 色・文字サイズ・余白・角丸のトークン（light / dark） |
| `src/styles/base.css` | 土台と共通部品（`.chip` `.tip` `.panel` `.empty-state`） |
| `src/composables/useThemeMode.ts` | ライト / ダーク / システムに従う の切り替えと保存 |
| `src/components/ThemeToggle.vue` | テーマ切り替えボタン |
| `public/theme-init.js` | 起動前にテーマを復元し、初回描画のちらつきを防ぐ |
| `scripts/check-contrast.mjs` | `npm run check:contrast`。配色が WCAG AA を満たすか検査 |
| `src/main.ts` | トークンの読み込み、Vuetify のテーマと `defaults`、ルータ |
| `src/types.ts` | API レスポンス型の置き場（雛形） |
| `src/composables/useAuth.ts` | ログイン状態と権限の取得 |
| `src/views/HomeView.vue` | 権限で表示を切り替える画面の例 |

## ディレクトリの役割

```
src/
├── main.ts          # トークン読み込み・Vuetify テーマ・defaults・ルータ
├── App.vue          # <v-app> と <router-view> だけ
├── styles/          # tokens.css（値）と base.css（共通部品）。見た目の値はここだけ
├── types.ts         # API の型。バックエンドを変えたらここも直す
├── composables/     # 状態と API 呼び出し。UI を持たない
├── components/      # 再利用する UI 部品・ダイアログ
└── views/           # ルータに紐づく画面
```

**composable と component の境界がこの構成の肝。**
`fetch` を書いてよいのは composable だけにする。コンポーネントに `fetch` を書き始めると、
同じ API を複数箇所から叩くようになり、ローディング表示やエラー処理が場所ごとにばらつく。

## composable の書き方

2 つの型がある。使い分けを間違えると、状態が共有されない／されすぎるバグになる。

### 画面ローカルな状態 — 関数の中で `ref` を作る

一覧の取得のように、その画面だけが持つ状態。呼ぶたびに新しい状態ができる。

```ts
import { ref } from 'vue'
import type { UserRecord } from '@/types'

export function useUsers() {
  const users   = ref<UserRecord[]>([])
  const loading = ref(false)
  const error   = ref<string | null>(null)

  async function fetchUsers(): Promise<void> {
    loading.value = true
    error.value   = null
    try {
      const res = await fetch('/api/users')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      users.value = await res.json() as UserRecord[]
    } catch (e) {
      error.value = String(e)
    } finally {
      loading.value = false
    }
  }

  // 更新系は「実行 → 再取得」で締める。楽観的更新より単純で、ズレが起きない。
  async function createUser(input: UserRecordInput): Promise<UserRecord> {
    const res  = await fetch('/api/users', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(input),
    })
    const data = await res.json() as UserRecord & { error?: string }
    if (!res.ok) throw new Error(data.error ?? `HTTP ${res.status}`)
    await fetchUsers()
    return data
  }

  return { users, loading, error, fetchUsers, createUser }
}
```

`error` を `string | null` で持ち、`loading` と組にするのを全 composable で統一する。
そうすると画面側は `v-alert` と `:loading` を機械的に繋ぐだけで済む。

### アプリ全体で共有する状態 — モジュールスコープで `ref` を作る

ログイン中のユーザーのように、どこから呼んでも同じでなければ困るもの。

```ts
import { ref, readonly } from 'vue'

// export しないモジュールスコープの ref。useAuth() を何度呼んでも同一実体になる。
const user   = ref<User | null>(null)
const loaded = ref(false)

export function useAuth() {
  async function fetchMe(): Promise<void> { /* ... */ }

  // readonly で返すと、画面側からの直接代入を型で防げる。
  // 状態を変えたいときは必ずこの composable の関数を通ることになる。
  return { user: readonly(user), loaded: readonly(loaded), fetchMe }
}
```

## コンポーネントの書き方

```vue
<template>
  <v-card>
    <v-card-title class="d-flex align-center pa-5 pb-3">
      <v-icon icon="mdi-account-cog-outline" class="mr-2" />
      <span>ユーザー管理</span>
      <v-spacer />
      <v-btn color="primary" prepend-icon="mdi-account-plus-outline" @click="openCreate">
        ユーザーを追加
      </v-btn>
    </v-card-title>

    <v-divider />

    <v-card-text class="pa-5">
      <v-alert
        v-if="error"
        type="error"
        density="compact"
        closable
        class="mb-3"
        @click:close="error = null"
      >{{ error }}</v-alert>

      <v-data-table
        :headers="headers"
        :items="users"
        :loading="loading"
        density="comfortable"
        no-data-text="ユーザーがいません"
        items-per-page="20"
      >
        <template #item.actions="{ item }">
          <v-btn icon="mdi-pencil-outline" variant="text" size="small" @click="openEdit(item)" />
        </template>
      </v-data-table>
    </v-card-text>
  </v-card>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useUsers } from '@/composables/useUsers'

const { users, loading, error, fetchUsers } = useUsers()

const headers = [
  { title: 'メールアドレス', key: 'email' },
  { title: '表示名',        key: 'name' },
  { title: '操作',          key: 'actions', sortable: false },
] as const

onMounted(fetchUsers)
</script>
```

守ると効くところ:

- **`no-data-text` を必ず日本語で指定する。** 既定は英語で、利用者が戸惑う
- **`:loading` を繋ぐ。** 反応がないと利用者は連打する
- **アイコンは `mdi-*-outline` 系で統一する。** 混ぜると素人っぽく見える
- **破壊的な操作は `color="error"`＋確認ダイアログ。** 一覧から一発で消えないようにする
- **`defineProps` / `defineEmits` は型引数で書く。** `defineProps<{ modelValue: boolean }>()`

## ダイアログの開閉

親が `v-model` で開閉を制御する形に揃える。子の中で `ref` を持つと、
親から閉じられなくなって詰む。

```vue
<script setup lang="ts">
const props = defineProps<{ modelValue: boolean }>()
const emit  = defineEmits<{ 'update:modelValue': [value: boolean] }>()

const dialog = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})
</script>
```

## サイドメニューは畳めるようにする

画面の左にメニューを置くなら、**必ず畳めること**。畳んだ状態は記憶する。

一覧表が主体の業務画面では、横幅がそのまま読める列数になる。メニューが
240px 居座ると、その分だけ表が狭くなる。しかもメニューを見るのは画面を
移るときだけで、作業中はずっと邪魔をしている。

```vue
<template>
  <v-app-bar>
    <!-- 開閉の操作はアプリバーに置く。メニューの中に置くと、閉じたあと戻せない。 -->
    <v-app-bar-nav-icon :aria-label="open ? 'メニューを畳む' : 'メニューを開く'"
      @click="open = !open" />
    <v-app-bar-title>{{ title }}</v-app-bar-title>
  </v-app-bar>

  <!-- rail は「アイコンだけの細い帯」。完全に消すより、
       どこへ行けるかが見えたままになるぶん迷わない。 -->
  <v-navigation-drawer v-model="open" :rail="rail" :permanent="!mobile" :temporary="mobile">
    <v-list nav density="comfortable">
      <v-list-item v-for="item in items" :key="item.to" :to="item.to"
        :prepend-icon="item.icon" :title="item.title" />
    </v-list>
  </v-navigation-drawer>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { useDisplay } from 'vuetify'

const { mobile } = useDisplay()
// 畳んだかどうかは覚える。画面を移るたびに開き直ると、毎回畳むことになる。
const rail = ref(localStorage.getItem('nav-rail') === '1')
const open = ref(!mobile.value)
watch(rail, (value) => { try { localStorage.setItem('nav-rail', value ? '1' : '0') } catch {} })
</script>
```

守るところ。

- **狭い画面では最初から閉じる。** `useDisplay()` の `mobile` を見て `temporary` にする。
  `permanent` のままだと、スマートフォンで画面の半分をメニューが占める。
- **開閉の操作はアプリバーに置く。** メニューの中に閉じるボタンだけを置くと、
  閉じたあとに開く手段が無くなる。
- **畳んだ状態を `localStorage` に残す。** 覚えないと、画面を移るたびに開き直り、
  使う人が毎回畳むことになる。`try/catch` で囲む（プライベートモードで例外になる）。
- **項目が5つ以下ならメニューを作らない。** アプリバーにタブで並べたほうが、
  横幅も操作も減る。メニューは行き先が増えてから。

## 権限による出し分け

`useAuth()` の `permissions` を使い、**使えない機能はボタンごと隠す**。
押せるのにエラーが返る作りは、利用者に「壊れている」と受け取られる。

```vue
<v-btn v-if="permissions.can_manage_users" @click="openUserAdmin">ユーザー管理</v-btn>
```

ただしフロントの出し分けは体験のためであって、防御ではない。
バックエンド側で必ず同じ権限を検査する（`webapp-auth-security` 参照）。

## 見た目を揃える

画面が増えるほど、配色・文字サイズ・余白は放っておくとばらつく。
1 つ 1 つの判断はその場では妥当に見えるので、**書く前に段を決めておく**しかない。
値は `src/styles/tokens.css`、共通部品は `src/styles/base.css` にある。

### 段は先に決まっている

| 種類 | 段数 | トークン |
|---|---|---|
| 文字サイズ | 7 | `--fs-2xs` 11px 〜 `--fs-2xl` 25px。本文は `--fs-md` 14px、一覧・フォームは `--fs-sm` 13px |
| 太さ | 3 | `--fw-normal` 400 / `--fw-medium` 500 / `--fw-bold` 700 |
| 余白 | 4px 刻み | `--sp-1` 4px 〜 `--sp-10` 40px |
| 角丸 | 4 | `--radius-sm` 6px / `--radius-md` 10px / `--radius-lg` 14px / `--radius-pill` |
| 部品の高さ | 3 | `--control-sm` 32 / `--control-md` 40（既定） / `--control-lg` 48 |

**ここに無い値を使わない。** 5px や 7px、`font-size: 15px` を書き始めた時点で、
段は意味を失う。足りないと感じたら、まず既存の段で表現できないかを疑う。

### 色は役割で呼ぶ

`--teal-500` ではなく `--text-brand` のように役割で名前を付けている。
配色を変えるとき `tokens.css` だけ差し替えれば済むようにするため。

- **文字** — `--ink-1`（見出し）〜 `--ink-4`（ラベル）。番号が上がるほど控えめ。
  **`--ink-1` 〜 `--ink-4` はどの `--surface-*` の上でも WCAG AA を満たす**ので、
  背景ごとに濃さを確かめなくてよい
- **`--ink-muted` は文字に使わない。** AA を満たさない。区切りや装飾専用
- **面** — `--surface-page`（地）→ `--surface-card` → `--surface-subtle` / `--surface-muted`
- **状態は 3 つだけ** — brand（正常・完了）/ warn（注意・未確定）/ danger（エラー・破壊的操作）。
  増やすと利用者が意味を覚えられない

### ライト / ダークは既定で両方入っている

色は `light-dark(ライト値, ダーク値)` で **1 行に両方**書く。
ライトとダークを別ブロックに分けると、片方だけトークンを足して
もう一方で色が消える、という事故が起きる。1 行なら構造上あり得ない。

| `<html>` の状態 | 効くテーマ |
|---|---|
| 属性なし（**既定**） | **OS の設定に従う** |
| `data-theme="light"` | 常にライト |
| `data-theme="dark"` | 常にダーク |

**既定を「システムに従う」にしているのが肝。** 端末をダークにしている人は、
何も操作しなくても最初からダークで開ける。light / dark の 2 状態だけだと、
必ずどちらかを押させることになる。

切り替えは `useThemeMode()`（`system` → `light` → `dark` の 3 状態）。
選択は `localStorage` に残る。`ThemeToggle.vue` をアプリバーに置けば動く。

- **`color-scheme` を宣言してあるので、スクロールバーやネイティブの
  日付ピッカーなどブラウザ側が描く部品も一緒に切り替わる**
- **Vuetify のテーマも合わせる。** `App.vue` が `useTheme()` に反映している。
  片方だけ切り替わると、`v-card` は明るいのに周りは暗い画面になる
- **サイズ・余白・角丸はテーマで変えない。** 変えると同じ画面が別物になり、
  片方のテーマでしか確認されていないレイアウト崩れが生まれる
- 必要ブラウザは Chrome 123+ / Safari 17.5+ / Firefox 120+。
  古い環境が対象なら `light-dark()` をやめて 3 ブロックに展開する

**ちらつき対策のスクリプトをインラインで書かないこと。**
`public/theme-init.js` を `<script src>` で読んでいるのは、
`core/security_headers.py` の CSP が `script-src 'self'` で、
インラインスクリプトは実行を拒否されるため。

配色を変えたら `npm run check:contrast` を走らせる。light / dark の全組み合わせを
検査し、1 つでも 4.5:1 を割ると落ちる。上の AA 保証はこれで担保している。

### チップと補足枠

高さ・余白・文字サイズを 1 か所で決めておくのが肝。画面ごとに `padding` を書くと、
`3px 7px` / `5px 8px` / `0 8px` のように必ずばらけ、横に並べたとき高さが揃わない。

```vue
<span class="chip chip--warn">未確定</span>
<span class="chip chip--pill chip--brand">承認済み</span>

<p class="tip tip--warn">
  <v-icon icon="mdi-alert-outline" />
  <span>公開すると、閲覧権限のある利用者全員に見えます。</span>
</p>
```

| クラス | サイズ | 使いどころ |
|---|---|---|
| `.chip--sm` / `.chip` / `.chip--lg` | 高さ 20 / 24 / 28px | 表のセル内 / 一覧の行 / 見出しの隣 |
| `.tip` / `.tip--compact` | 余白 12 / 8px | 常設の説明 / 表のセル内・脚注 |

- **`.chip--pill` は状態バッジ専用。** 分類ラベルは角丸のままにして、
  「丸い＝いまの状態」と形で読み分けられるようにする
- **`.tip` と `v-alert` を使い分ける。** `v-alert` は「いま起きたこと」を伝える
  閉じられる通知。常設の説明に使うと、閉じるボタンの有無が画面ごとにぶれる
- **`.chip` と `v-chip` を混ぜない。** どちらかに寄せる

### いちばん効くのは Vuetify の `defaults`

画面ごとに `variant` や `density` を書かせると必ずばらける。
`main.ts` で既定を決めておけば、呼び出し側は何も書かなくてよくなる。

```ts
defaults: {
  VBtn:       { rounded: 'lg', elevation: 0, variant: 'flat' },
  VCard:      { rounded: 'lg', elevation: 0, border: true },
  VTextField: { variant: 'outlined', density: 'comfortable', hideDetails: 'auto' },
  VDataTable: { density: 'comfortable', itemsPerPage: 20 },
  VChip:      { size: 'small', variant: 'tonal' },
  VAlert:     { density: 'compact', variant: 'tonal' },
}
```

`hideDetails: 'auto'` は特に効く。付けないと、エラーが無いときも説明文用の
高さが確保され、フォームの行間が画面ごとにずれる。

呼び出し側で上書きしたくなったら、**「本当にその画面だけ違ってよいか」を先に疑う。**
たいていは `defaults` のほうを直すべき場面になっている。

### hex を書いてよいのは 2 ファイルだけ

Vuetify は `on-primary` などを自前で計算するため、テーマ色は hex で渡す必要がある。
そのため `main.ts` の `themeColors` と `tokens.css` に同じ値が 2 か所出る。
**片方だけ変えると、`v-btn` の色と手書き CSS の色が少しずつ違う画面ができる。**
対応関係は `main.ts` にコメントで併記してあるので、必ず同時に直す。

設計の考え方、トークンの足し方、既存アプリへの後付け手順は
`references/design-tokens.md` を読む。

## よくあるつまずき

Koyorinaへ渡す前に、`frontend/package.json` の `build` が `vite build` を実行することを
必ず確認する。`frontend/dist` 自体は成果物へ含めない。プレビュー環境がソースから初回
ビルドを完了してから稼働状態になるため、開発サーバーだけ動く構成は完成扱いにしない。

| 症状 | 原因と対処 |
|---|---|
| `npm run build` だけ落ちる | `build` は `vue-tsc --noEmit` を通す。`npm run typecheck` で先に確認する |
| Vuetify のコンポーネントが `Cannot find name` | `vite-plugin-vuetify` の autoImport 前提。`vite.config.ts` の plugins を確認 |
| dev で API が 404 | `vite.config.ts` の `server.proxy` に対象パスを足す |
| dev でログインが切れる | Cookie の Origin 不一致。Vite は 5173、backend は 8080。プロキシ経由で叩く |
| dark テーマで表が読めない | `styles/base.css` で `.v-theme--dark` の表ヘッダを底上げ済み。読み込み順を確認する |
| dark にすると一部の色が消える | `light-dark()` の第 2 引数を書き忘れている |
| OS がダークなのにライトで出る | `<html>` に `data-theme` が残っている。テーマを「システム」に戻す |
| 読み込み時に一瞬ライトになる | `public/theme-init.js` が読めていない。CSP で弾かれていないか確認する |
| 周りは暗いのに `v-card` だけ明るい | Vuetify のテーマが追随していない。`App.vue` の `watch` を確認する |
| 同じ行のチップの高さが揃わない | `.chip` と `v-chip` が混在している。どちらかに寄せる |
| フォームの行間が画面ごとに違う | 入力欄に `hideDetails` が無い。`main.ts` の `defaults` で付ける |

## 参照ファイル

- `references/design-tokens.md` — トークンの一覧と設計方針、チップ／補足枠のサイズ、既存アプリへの後付け手順
- `references/patterns.md` — フォーム検証、長時間処理の進捗表示、Markdown 描画などの実装レシピ
