<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '@/composables/useApi'
import { useAuth } from '@/composables/useAuth'
import { useCollaborators } from '@/composables/useCollaborators'
import type { Project, Tenant } from '@/types'

const props = defineProps<{ project: Project }>()
const { user } = useAuth()
// 変わった結果を親へ返す。返さないと、画面が持っている古いアプリの情報のまま
// 表示が残り、移したのにラベルが変わらないように見える。
const emit = defineEmits<{ updated: [project: Project] }>()
const { collaborators, candidates, canAdminister, loading, error, refresh, add, remove, handOver } =
  useCollaborators(props.project.id)
const handing = ref<{ user_id: string; name: string }>()
// 置き場のテナント。利用量を数える単位なので、動かせるのはオーナーと管理者だけ。
const tenants = ref<Tenant[]>([])
// いま属しているテナント。移したらここを書き換えるので、表示もすぐ追いつく。
const current = ref<string | null | undefined>(props.project.tenant_id)
const tenantId = ref<string | null | undefined>(props.project.tenant_id)
const moving = ref(false)
const moveReason = ref('')
const migrationLoading = ref(false)
const migrationNotice = ref('')
const nameOf = (id?: string | null) => tenants.value.find(t => t.id === id)?.name ?? '不明'
const tenantName = computed(() => nameOf(current.value))
const tenantOptions = computed(() => tenants.value
  .filter(t => t.enabled || t.id === current.value)
  .map(t => ({ title: t.name, value: t.id })))
async function confirmMove() {
  if (!tenantId.value || tenantId.value === current.value || moveReason.value.trim().length < 10) return
  migrationLoading.value = true
  migrationNotice.value = '生成・プレビューを停止し、データを検証しながら移しています。'
  try {
    const started = await api<{ id: string }>(`/api/admin/projects/${props.project.id}/tenant-migrations`,
      'POST', { target_tenant_id: tenantId.value, reason: moveReason.value.trim() })
    let state: { status: string; error?: string } = { status: 'copying' }
    for (let attempt = 0; attempt < 210 && state.status === 'copying'; attempt += 1) {
      await new Promise(resolve => window.setTimeout(resolve, 2000))
      state = await api(`/api/admin/tenant-migrations/${started.id}`)
    }
    if (state.status !== 'completed') throw new Error(state.error || '移行を完了できませんでした。')
    // 移行はここで済んでいる。取り直しに失敗しても「失敗」と見せず、ダイアログを閉じる。
    moving.value = false
    moveReason.value = ''
    migrationNotice.value = 'テナント移行が完了しました。移行元は7日間、復旧用に保持されます。'
    try {
      const project = await api<Project>(`/api/projects/${props.project.id}`)
      current.value = project.tenant_id
      tenantId.value = project.tenant_id
      emit('updated', project)
    } catch {
      migrationNotice.value += ' 表示を最新にするには、画面を再読み込みしてください。'
    }
  } catch (e) {
    migrationNotice.value = ''
    error.value = e instanceof Error ? e.message : 'テナントを移せません。'
  } finally { migrationLoading.value = false }
}
const picked = ref<string>()
const choices = computed(() => candidates.value.map(item => ({
  title: `${item.name}（${item.email}）`, value: item.user_id })))
const removing = ref<{ user_id: string; name: string }>()
async function invite() {
  if (!picked.value) return
  await add(picked.value)
  picked.value = undefined
}
async function confirmHandOver() {
  const target = handing.value
  handing.value = undefined
  if (!target) return
  const project = await handOver(target.user_id)
  if (project) emit('updated', project)
}
async function confirmRemove() {
  const target = removing.value
  removing.value = undefined
  if (target) await remove(target.user_id)
}
onMounted(async () => {
  await refresh()
  try { tenants.value = await api<Tenant[]>('/api/tenants') } catch { tenants.value = [] }
})
</script>

<template>
  <section class="mt-5">
    <v-alert v-if="error" type="error" class="mb-4">{{ error }}</v-alert>

    <h2>テナント</h2>
    <p class="my-3">このアプリのデータを分離して保存する事業単位です。
      利用者は所属するテナントのアプリだけを扱えます。</p>
    <v-alert v-if="migrationNotice" type="info" variant="tonal" class="my-3">{{ migrationNotice }}</v-alert>
    <div class="tenant my-4">
      <span class="chip chip--pill chip--brand">{{ tenantName }}</span>
      <template v-if="user?.can_manage_users && tenantOptions.length > 1">
        <v-select v-model="tenantId" :items="tenantOptions" label="移す先" density="compact"
          hide-details class="tenant-choice" />
        <v-btn variant="outlined" :disabled="loading || tenantId === current"
          @click="moving = true">テナントを移す</v-btn>
      </template>
      <span v-else-if="user?.can_manage_users" class="meta">移せるテナントが他にありません。</span>
      <span v-else class="meta">テナント移行はプラットフォーム管理者が行います。</span>
    </div>

    <v-divider class="my-6" />

    <h2>共同開発者</h2>
    <p class="my-3">
      追加した人は、仕様の編集・生成・プレビューをオーナーと同じようにできます。
      <strong>アプリの削除と、この共有設定の変更はできません。</strong>
    </p>
    <p class="tip my-3">
      <v-icon icon="mdi-information-outline" />
      <span>生成は依頼した人自身の実行環境で動きます。Codexを使う場合も、
        消費されるのはその人自身のChatGPTの利用枠です。</span>
    </p>

    <div v-if="canAdminister" class="invite">
      <v-select v-model="picked" :items="choices" label="追加する利用者"
        :disabled="loading || !choices.length"
        :no-data-text="'追加できる利用者がいません。'" class="invite-choice" />
      <v-btn color="primary" :disabled="!picked" :loading="loading" @click="invite">追加</v-btn>
    </div>

    <v-table v-if="collaborators.length" density="comfortable" class="my-4">
      <thead><tr><th>名前</th><th>メールアドレス</th><th v-if="canAdminister">操作</th></tr></thead>
      <tbody>
        <tr v-for="member in collaborators" :key="member.user_id">
          <td>{{ member.name }}</td>
          <td>{{ member.email }}</td>
          <td v-if="canAdminister" class="row-actions">
            <v-btn variant="outlined" :disabled="loading"
              @click="handing = { user_id: member.user_id, name: member.name }">オーナーにする</v-btn>
            <v-btn variant="outlined" color="error" :disabled="loading"
              @click="removing = { user_id: member.user_id, name: member.name }">外す</v-btn>
          </td>
        </tr>
      </tbody>
    </v-table>
    <p v-else class="tip my-4">
      <v-icon icon="mdi-account-multiple-outline" />
      <span>まだ共同開発者はいません。このアプリを触れるのはオーナーだけです。</span>
    </p>

    <p v-if="!canAdminister" class="meta">共有の設定を変えられるのは、オーナーと管理者だけです。</p>

    <v-dialog v-model="moving" max-width="520">
      <v-card class="pa-6">
        <h2>テナントを移しますか？</h2>
        <p class="my-4">
          <strong>{{ tenantName }}</strong> から <strong>{{ nameOf(tenantId) }}</strong> へ移します。
          生成とプレビューを停止し、コード、ジョブ、履歴、プレビューデータをコピーして
          検証できた場合だけ所属先を切り替えます。
        </p>
        <p class="mb-4">公開アプリがある場合は、先に「公開アプリ運用」で停止してください。
          公開データとビルド履歴は保持します。移動後は利用者・部門の許可が解除されるため、
          移動先の運用管理者が設定し直してください。</p>
        <v-textarea v-model="moveReason" label="移行理由" rows="2" maxlength="500"
          hint="監査ログへ記録します（10文字以上）" persistent-hint />
        <v-card-actions class="actions">
          <v-btn variant="outlined" @click="moving = false">キャンセル</v-btn>
          <v-btn color="primary" :loading="migrationLoading" :disabled="moveReason.trim().length < 10"
            @click="confirmMove">移行を開始</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog :model-value="!!handing" max-width="520"
      @update:model-value="value => { if (!value) handing = undefined }">
      <v-card class="pa-6">
        <h2>オーナーを「{{ handing?.name }}」さんに引き継ぎますか？</h2>
        <p class="my-4">あなたは共同開発者として残り、編集と生成は続けられます。
          ただし<strong>アプリの削除と共有設定の変更はできなくなります</strong>。
          生成コードと履歴はそのまま引き継がれます。</p>
        <v-card-actions>
          <v-btn variant="outlined" @click="handing = undefined">キャンセル</v-btn>
          <v-btn color="primary" :loading="loading" @click="confirmHandOver">引き継ぐ</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog :model-value="!!removing" max-width="520"
      @update:model-value="value => { if (!value) removing = undefined }">
      <v-card class="pa-6">
        <h2>「{{ removing?.name }}」を外しますか？</h2>
        <p class="my-4">このアプリの仕様・生成・プレビューを扱えなくなります。
          これまでの生成履歴は残ります。</p>
        <v-card-actions>
          <v-btn variant="outlined" @click="removing = undefined">キャンセル</v-btn>
          <v-btn color="error" :loading="loading" @click="confirmRemove">外す</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </section>
</template>

<style scoped>
.tenant { display: flex; gap: var(--sp-3); align-items: center; flex-wrap: wrap; }
.tenant-choice { min-width: 14rem; max-width: 20rem; }
.row-actions { display: flex; gap: var(--sp-2); align-items: center; flex-wrap: wrap; vertical-align: middle; }
.row-actions :deep(.v-btn) { margin-block: var(--sp-1); }
.invite { display: flex; gap: var(--sp-3); align-items: center; flex-wrap: wrap; margin-block: var(--sp-4); }
.invite-choice { width: 28rem; max-width: 100%; }
.meta { color: var(--ink-4); font-size: var(--fs-xs); }
</style>
