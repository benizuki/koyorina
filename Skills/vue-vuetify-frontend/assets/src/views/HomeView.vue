<template>
  <v-app-bar flat color="surface">
    <v-app-bar-title>__APP_TITLE__</v-app-bar-title>
    <v-spacer />
    <template v-if="user">
      <span class="meta-text mr-2">{{ user.name }}</span>
      <!-- ロールは分類なので角丸のチップ。状態を示すときだけ chip--pill にする -->
      <span class="chip chip--brand mr-3">{{ user.role }}</span>
      <v-btn
        v-if="permissions.can_manage_users"
        variant="text"
        prepend-icon="mdi-account-cog-outline"
        @click="userAdminOpen = true"
      >ユーザー管理</v-btn>
      <v-btn variant="text" prepend-icon="mdi-logout" href="/logout">ログアウト</v-btn>
    </template>
    <!-- ログインしていなくても押せる位置に置く。ログイン画面が眩しい、を防ぐ -->
    <ThemeToggle />
  </v-app-bar>

  <v-main>
    <!-- /api/me の応答前に本体を描くと、権限のないボタンが一瞬見える。
         loaded を待ってから描画する。 -->
    <v-container v-if="loaded" class="py-6">
      <v-card>
        <v-card-title class="pa-5 pb-3">
          <h1 class="page-title">ようこそ</h1>
        </v-card-title>
        <v-divider />
        <v-card-text class="pa-5 d-flex flex-column ga-4">
          <p class="page-lead">ここに本体の画面を作る。</p>

          <!-- 常設の説明は .tip を使う。閉じられる通知は v-alert。
               混ぜると、閉じるボタンの有無が画面ごとにぶれる -->
          <p v-if="!permissions.can_edit" class="tip tip--warn">
            <v-icon icon="mdi-lock-outline" />
            <span>閲覧のみの権限です。編集が必要な場合は管理者に依頼してください。</span>
          </p>
        </v-card-text>
      </v-card>
    </v-container>

    <v-container v-else class="py-6 d-flex justify-center">
      <v-progress-circular indeterminate color="primary" />
    </v-container>
  </v-main>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useAuth } from '@/composables/useAuth'
import ThemeToggle from '@/components/ThemeToggle.vue'

const { user, permissions, loaded, fetchMe } = useAuth()
const userAdminOpen = ref(false)

onMounted(fetchMe)
</script>
