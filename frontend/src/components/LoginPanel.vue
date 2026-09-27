<!-- ログイン画面。左に糸をより合わせる絵、右に銘とログインだけを置く。
     ここへ来た人がする判断は「ログインするか」しかないので、他のものを並べない。

     ダークは夜景につながる濃い青緑、ライトは春の空気になじむ淡い青緑にする。
     配色（data-palette）ではなく、ログイン画の明暗テーマに合わせる。 -->
<template>
  <div class="login-scene">
    <LoginAmbient />
    <div class="login" :class="{ 'login--light': effectiveTheme === 'light' }">
      <div class="login__art" role="presentation">
        <img :src="loginArt" alt="" />
      </div>
      <div class="login__body">
        <div class="login__content">
          <div class="login__brand">
            <KoyoriLogo :size="52" />
            <div>
              <img class="login__wordmark" :src="wordmark" :alt="APP_NAME" />
            </div>
          </div>
          <p class="login__lead">小さなアイデアをより合わせ、役立つアプリに。</p>
          <div class="login__action"><slot /></div>
          <p v-if="!configured" class="login__warn">
            Googleログインは未設定です。管理者に設定を依頼してください。
          </p>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import darkLoginArt from '@/assets/brand-login-art.png'
import lightLoginArt from '@/assets/brand-login-art-light.png'
import wordmark from '@/assets/brand-wordmark.png'
import KoyoriLogo from '@/components/KoyoriLogo.vue'
import LoginAmbient from '@/components/LoginAmbient.vue'
import { APP_NAME } from '@/branding'
import { useThemeMode } from '@/composables/useThemeMode'

defineProps<{ configured: boolean }>()

const { effectiveTheme } = useThemeMode()
const loginArt = computed(() => effectiveTheme.value === 'light' ? lightLoginArt : darkLoginArt)
</script>

<style scoped>
.login-scene {
  position: relative;
  isolation: isolate;
  display: grid;
  /* 背景は画面の端まで敷く。器の幅で切ると、空や木立ではなく
     カードの後ろに置かれた板に見える。 */
  margin-inline: calc(50% - 50vw);
  padding-inline: calc(50vw - 50%);
  /* 画面の高さいっぱいまで伸ばす。途中で切ると、そこから下が
     ただの地色になり、空や木立が続いていないのが分かってしまう。 */
  min-height: calc(100svh - 9rem);
  padding-block: var(--sp-10);
  overflow: hidden;
}
.login {
  position: relative;
  z-index: 1;
  display: grid;
  grid-template-columns: 1fr 1fr;
  max-width: 940px;
  width: 100%;
  margin: auto;
  border: 1px solid var(--border-inverse);
  border-radius: var(--radius-lg);
  overflow: hidden;
  box-shadow: var(--shadow-overlay);
}
/* 絵は面積が変わっても中身の比率を保つ。伸ばすと金床だけ歪む。 */
.login__art { min-height: 420px; background: var(--surface-login); }
.login__art img { display: block; width: 100%; height: 100%; object-fit: cover; object-position: left center; }

.login__body {
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: var(--sp-10) var(--sp-8);
  background: var(--surface-login);
  color: var(--text-on-inverse);
}
.login__content {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: var(--sp-4);
}
.login__brand { display: flex; align-items: center; gap: var(--sp-3); color: var(--text-on-inverse); }
.login__wordmark { display: block; width: min(270px, 60vw); height: auto; }
/* 読みが割れる名前なので、銘の下に一度だけ添える。 */
.login__lead { margin: 0; font-size: var(--fs-lg); font-weight: var(--fw-medium); }
/* Googleが描くボタンをそのまま置く領域。高さは既定の部品と揃える。 */
.login__action { min-height: var(--control-md); margin-top: var(--sp-2); }
.login__warn { margin: 0; font-size: var(--fs-sm); color: var(--text-warn); }

/* ライト画像の空と湖へつながる、わずかに青みのある生成りの面。
   文字は通常の濃いインクへ戻し、白い銘はフィルタで深い常磐色にする。 */
.login--light { border-color: var(--border-default); }
.login--light .login__body {
  background: linear-gradient(135deg, #edf5f1 0%, #d7e9e4 100%);
  color: var(--ink-1);
}
.login--light .login__brand { color: var(--ink-1); }
.login--light .login__wordmark {
  filter: brightness(0) saturate(100%) invert(20%) sepia(15%) saturate(1280%) hue-rotate(128deg) brightness(88%) contrast(88%);
}

/* 横に並べられない幅では、絵を上に敷いて帯にする。 */
@media (max-width: 860px) {
  .login-scene { min-height: 0; padding-block: var(--sp-6); }
  .login { grid-template-columns: 1fr; }
  .login__art { min-height: 0; height: 180px; }
  .login__body { padding: var(--sp-8) var(--sp-6); }
}
</style>
