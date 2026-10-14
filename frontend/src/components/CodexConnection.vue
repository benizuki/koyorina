<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useCodex } from '@/composables/useCodex'
import { APP_NAME } from '@/branding'
const { status, loading, error, refresh, login, logout } = useCodex()
const disconnect = ref(false), forget = ref(false)
async function confirmDisconnect() { await logout(); disconnect.value = false }
// 共通キーの設定のときは、各自のChatGPTのログイン情報だけを消す（共通キーはそのまま）。
async function confirmForget() { await logout(); forget.value = false }
// 認証コードの期限。テンプレートの v-else-if の絞り込みが式の中まで届かないので、ここで求める。
const loginExpiresAt = computed(() => {
  const expires = status.value?.login?.expires_at
  return expires ? new Date(expires).toLocaleTimeString('ja-JP') : ''
})
onMounted(refresh)
</script>

<template>
  <v-card class="pa-6 mb-5">
    <h2>本人のCodexアカウント</h2>
    <p v-if="status?.auth_mode === 'shared'" class="my-4">コード生成には運営の共通APIキーを使う設定です。ChatGPTアカウントの接続は不要です。</p>
    <p v-else class="my-4">{{ APP_NAME }}へのGoogleログインとは別に、コード生成に使うChatGPTアカウントを接続します。運営者のAPIキーは使いません。</p>
    <v-alert v-if="error || status?.error" type="error" class="mb-4">{{ error || status?.error }}</v-alert>
    <template v-if="status?.status === 'connected' && status.auth_type === 'apiKey'">
      <span class="chip chip--brand">共通APIキーで利用中</span>
      <p class="tip my-4">生成時は運営の共通APIキーを使用します。</p>
      <template v-if="status.individual_login">
        <p class="mb-4">以前に接続したChatGPTアカウントのログイン情報が保存されています。各自の認証に戻ったときに使われます。</p>
        <v-btn variant="outlined" color="error" class="mb-4" :disabled="loading || status.busy" @click="forget = true">
          ChatGPTのログイン情報を削除</v-btn>
      </template>
    </template>
    <template v-else-if="status?.status === 'connected'">
      <span class="chip chip--brand">接続済み</span>
      <p class="my-4">{{ status.email || 'ChatGPTアカウント' }}<span v-if="status.plan"> ／ {{ status.plan }}</span></p>
      <p class="tip mb-4">生成時はこのアカウントの利用枠を使用します。利用枠やアカウントによって実行できない場合があります。</p>
      <v-btn variant="outlined" color="error" :disabled="loading || status.busy" @click="disconnect = true">接続を解除</v-btn>
    </template>
    <template v-else-if="status?.status === 'pending' && status.login">
      <p class="tip tip--warn">次の認証コードを、ChatGPTの認証画面に入力してください。コードを他の人へ共有しないでください。</p>
      <v-text-field :model-value="status.login.user_code" label="認証コード" readonly class="my-4" />
      <v-btn color="primary" href="https://auth.openai.com/codex/device" target="_blank" rel="noopener noreferrer">ChatGPTの認証画面を開く</v-btn>
      <p class="my-4">認証が終わると自動で接続状態を更新します。期限の目安：{{ loginExpiresAt }}</p>
      <p class="mb-4">デバイス認証を利用できない場合は、ChatGPTのセキュリティ設定や組織の許可を確認してください。</p>
      <v-btn variant="text" :loading="loading" @click="logout">認証をキャンセル</v-btn>
    </template>
    <template v-else>
      <p v-if="status?.status === 'unavailable'" class="tip tip--warn mb-4">AppGenの実行環境が未設定です。管理者による接続設定が必要です。</p>
      <p v-else-if="status?.status === 'preparing'" class="tip mb-4">
        あなた専用の実行環境を準備しています。数十秒お待ちください。以前に接続していれば、
        そのまま接続済みに戻ります。まだの場合は、準備ができ次第ログインを始めます。
      </p>
      <p v-if="status?.auth_mode === 'shared' && status.status === 'disconnected'" class="tip tip--warn mb-4">
        共通APIキーで接続できませんでした。管理者にキーの設定を確認してください。</p>
      <v-btn v-else color="primary" :loading="loading" :disabled="!status || ['unavailable', 'preparing'].includes(status.status)" @click="login">Codexへログイン</v-btn>
    </template>
    <v-btn variant="text" class="ml-3" :disabled="loading" @click="refresh">接続状態を更新</v-btn>
    <v-dialog v-model="disconnect" max-width="560"><v-card class="pa-6">
      <h2>Codexの接続を解除しますか？</h2><p class="my-4">次回生成時にChatGPTでの再認証が必要です。作成済みコードは削除しません。</p>
      <v-card-actions><v-btn @click="disconnect = false">キャンセル</v-btn><v-btn color="error" :loading="loading" @click="confirmDisconnect">接続を解除</v-btn></v-card-actions>
    </v-card></v-dialog>
    <v-dialog v-model="forget" max-width="560"><v-card class="pa-6">
      <h2>ChatGPTのログイン情報を削除しますか？</h2><p class="my-4">共通APIキーでの生成はこのまま使えます。各自の認証に戻ったときは、ChatGPTでの再認証が必要です。作成済みコードは削除しません。</p>
      <v-card-actions><v-btn @click="forget = false">キャンセル</v-btn><v-btn color="error" :loading="loading" @click="confirmForget">削除する</v-btn></v-card-actions>
    </v-card></v-dialog>
  </v-card>
</template>
