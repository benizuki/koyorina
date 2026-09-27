<template>
  <v-app>
    <!-- 認証をスキップしている間は常時バナーを出す。
         「ローカルだから」で消すと、そのまま気付かず公開する事故が起きる。 -->
    <div v-if="authMode === 'dev-bypass'" class="dev-banner">
      <v-icon icon="mdi-shield-off-outline" size="16" />
      <span>開発モード：Google ログインを省略しています（{{ user?.name }} / {{ role }}）。この状態で公開しないでください。</span>
    </div>

    <router-view />
  </v-app>
</template>

<script setup lang="ts">
import { watch } from 'vue'
import { useTheme } from 'vuetify'
import { useAuth } from '@/composables/useAuth'
import { useThemeMode } from '@/composables/useThemeMode'

// tokens.css（CSS 変数）と Vuetify のテーマを合わせる。
// 片方だけ切り替わると、v-card は明るいのに周りは暗い、という画面になる。
// data-theme 属性は useThemeMode が付け外しするので、ここでは Vuetify 側だけ追随させる。
const theme = useTheme()
const { effectiveTheme } = useThemeMode()
watch(effectiveTheme, (next) => { theme.global.name.value = next }, { immediate: true })

const { user, role, authMode } = useAuth()
</script>

<style>
/* 全体に効く見た目は styles/base.css に置く。
   ここに増やし始めると、トークンを通さない色やサイズが混ざる。 */
.dev-banner {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-4);
  color: var(--text-warn);
  background: var(--surface-warn);
  border-bottom: 1px solid var(--border-warn);
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  text-align: center;
}
</style>
