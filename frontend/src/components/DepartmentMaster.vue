<script setup lang="ts">
// 所属組織の階層。左に木、右に追加・編集の欄を置く。
// 上位の組織に付けたアプリの利用許可は配下へ届くので、木の形がそのまま権限の範囲になる。
import { computed, ref, watch } from 'vue'
import type { Department, DepartmentKind, ManagedUser } from '@/types'

const props = defineProps<{
  departments: Department[]
  users: ManagedUser[]
  loading: boolean
  save: (department: Partial<Department>) => Promise<boolean | undefined>
  remove: (department: Department) => Promise<boolean | undefined>
  refresh: () => Promise<unknown>
}>()

const KIND_NAMES: Record<DepartmentKind, string> = { division: '部門', section: '課', team: 'チーム' }
const kindChoices = (Object.keys(KIND_NAMES) as DepartmentKind[]).map(value => ({ title: KIND_NAMES[value], value }))
// 配下に足すときの種別の初期値。部門の下は課、課の下はチーム。
const CHILD_KIND: Record<DepartmentKind, DepartmentKind> = { division: 'section', section: 'team', team: 'team' }

const byId = computed(() => new Map(props.departments.map(d => [d.id, d])))
const children = computed(() => {
  const map = new Map<string | null, Department[]>()
  for (const d of props.departments) {
    const parent = d.parent_id && byId.value.has(d.parent_id) ? d.parent_id : null
    map.set(parent, [...(map.get(parent) ?? []), d])
  }
  return map
})
const members = computed(() => {
  const count = new Map<string, number>()
  for (const user of props.users) if (user.department_id) count.set(user.department_id, (count.get(user.department_id) ?? 0) + 1)
  return count
})
function descendants(id: string): Set<string> {
  const found = new Set<string>()
  const pending = [...(children.value.get(id) ?? [])]
  while (pending.length) {
    const current = pending.pop()!
    if (found.has(current.id)) continue
    found.add(current.id)
    pending.push(...(children.value.get(current.id) ?? []))
  }
  return found
}

// 閉じた組織だけを覚える。新しく足した組織は開いたまま見える。
const collapsed = ref(new Set<string>())
function toggle(id: string) {
  const next = new Set(collapsed.value)
  if (next.has(id)) next.delete(id); else next.add(id)
  collapsed.value = next
}
type Row = { department: Department; depth: number; hasChildren: boolean }
// API は親の直後に子が来る順で返す。閉じた組織の配下だけを飛ばす。
const rows = computed<Row[]>(() => {
  const result: Row[] = []
  const walk = (parent: string | null, depth: number) => {
    for (const department of children.value.get(parent) ?? []) {
      const hasChildren = (children.value.get(department.id) ?? []).length > 0
      result.push({ department, depth, hasChildren })
      if (hasChildren && !collapsed.value.has(department.id)) walk(department.id, depth + 1)
    }
  }
  walk(null, 0)
  return result
})

const form = ref<Partial<Department>>({})
const selectedId = computed(() => form.value.id)
function nextOrder(parentId: string | null) {
  const siblings = children.value.get(parentId) ?? []
  return siblings.length ? Math.max(...siblings.map(d => d.sort_order)) + 1 : 0
}
function startAdd(parent?: Department) {
  const parentId = parent?.id ?? null
  if (parent) { const next = new Set(collapsed.value); next.delete(parent.id); collapsed.value = next }
  form.value = { name: '', note: '', enabled: true, parent_id: parentId,
                 kind: parent ? CHILD_KIND[parent.kind] : 'division', sort_order: nextOrder(parentId) }
}
function startEdit(department: Department) { form.value = { ...department } }
startAdd()
// 保存や削除で一覧が変わったら、編集中の組織を最新の内容に合わせる。消えていれば追加に戻す。
watch(() => props.departments, () => {
  if (form.value.id && !byId.value.has(form.value.id)) startAdd()
})

const parentChoices = computed(() => {
  const blocked = form.value.id ? new Set([form.value.id, ...descendants(form.value.id)]) : new Set<string>()
  return [{ title: '（最上位）', value: null as string | null },
    ...props.departments.filter(d => !blocked.has(d.id)).map(d => ({ title: d.path, value: d.id as string | null }))]
})
const formHeading = computed(() => form.value.id ? '組織を編集' : '組織を追加')
const formLead = computed(() => {
  const parent = form.value.parent_id ? byId.value.get(form.value.parent_id) : undefined
  if (form.value.id) return parent ? `${parent.path} の配下にあります` : '最上位の組織です'
  return parent ? `${parent.path} の配下に追加します` : '最上位の組織として追加します'
})
async function submit() {
  const editing = !!form.value.id
  if (await props.save(form.value)) {
    if (editing) {
      const saved = props.departments.find(d => d.id === form.value.id)
      if (saved) startEdit(saved)
    } else startAdd(form.value.parent_id ? byId.value.get(form.value.parent_id) : undefined)
  }
}

const removing = ref<Department>()
const removable = (department: Department) =>
  !(children.value.get(department.id) ?? []).length && !members.value.get(department.id)
async function applyRemoval() {
  if (removing.value && await props.remove(removing.value)) removing.value = undefined
}
</script>

<template>
  <div class="page-heading mb-4">
    <p>利用者の所属を表す組織です。部門・課・チームを階層で登録します。
      アプリの利用許可を上位の組織に付けると、配下の組織に所属する利用者も使えます。</p>
    <div class="heading-actions">
      <v-btn variant="outlined" prepend-icon="mdi-refresh" :disabled="loading" @click="refresh">再読み込み</v-btn>
      <v-btn color="primary" prepend-icon="mdi-plus" :disabled="loading" @click="startAdd()">最上位の組織を追加</v-btn>
    </div>
  </div>

  <div class="org-layout">
    <v-card class="pa-5">
      <h2 class="section-title">組織の階層</h2>
      <p class="meta-text mt-1">行を押すと右側で編集できます。名前の左の記号で開閉します。</p>
      <ul v-if="rows.length" class="tree mt-4" role="tree" aria-label="組織の階層">
        <li v-for="row in rows" :key="row.department.id" role="treeitem"
          :aria-level="row.depth + 1" :aria-expanded="row.hasChildren ? !collapsed.has(row.department.id) : undefined"
          :aria-selected="selectedId === row.department.id"
          class="tree-row" :class="{ 'is-selected': selectedId === row.department.id, 'is-off': !row.department.enabled }"
          :style="{ '--depth': row.depth }">
          <span v-for="level in row.depth" :key="level" class="guide" :style="{ '--level': level }" aria-hidden="true" />
          <button v-if="row.hasChildren" type="button" class="toggle"
            :aria-label="`${row.department.name}を${collapsed.has(row.department.id) ? '開く' : '閉じる'}`"
            @click="toggle(row.department.id)">
            <v-icon size="18" :icon="collapsed.has(row.department.id) ? 'mdi-chevron-right' : 'mdi-chevron-down'" />
          </button>
          <span v-else class="leaf" aria-hidden="true" />
          <button type="button" class="tree-main" @click="startEdit(row.department)">
            <span class="tree-name">{{ row.department.name }}</span>
            <span class="chip chip--sm chip--info">{{ KIND_NAMES[row.department.kind] }}</span>
            <span v-if="members.get(row.department.id)" class="meta-text">所属 {{ members.get(row.department.id) }}名</span>
            <span v-if="!row.department.enabled" class="chip chip--sm chip--warn">停止中</span>
            <span v-if="row.department.note" class="meta-text note">{{ row.department.note }}</span>
          </button>
          <span class="tree-actions">
            <v-tooltip text="配下に組織を追加">
              <template #activator="{ props: tip }">
                <v-btn v-bind="tip" icon="mdi-plus" variant="text" size="small" :disabled="loading"
                  :aria-label="`${row.department.name}の配下に組織を追加`" @click="startAdd(row.department)" />
              </template>
            </v-tooltip>
            <v-tooltip :text="removable(row.department) ? '削除' : '配下の組織か所属者がいるため削除できません'">
              <template #activator="{ props: tip }">
                <!-- 押せない理由を読めるよう、無効のボタンを span で包んでツールチップを出す -->
                <span v-bind="tip">
                  <v-btn icon="mdi-delete-outline" variant="text" size="small" color="error"
                    :disabled="loading || !removable(row.department)"
                    :aria-label="`${row.department.name}を削除`" @click="removing = row.department" />
                </span>
              </template>
            </v-tooltip>
          </span>
        </li>
      </ul>
      <p v-else class="tip mt-4">まだ組織がありません。「最上位の組織を追加」から登録してください。</p>
    </v-card>

    <v-card class="pa-5 org-form">
      <h2 class="section-title">{{ formHeading }}</h2>
      <p class="meta-text mt-1">{{ formLead }}</p>
      <div class="form-fields">
        <v-text-field v-model="form.name" label="名称" />
        <v-select v-model="form.kind" :items="kindChoices" label="種別" />
        <v-select v-model="form.parent_id" :items="parentChoices" label="上位の組織"
          hint="ここを変えると組織を別の階層へ移せます。自分自身と配下は選べません。" persistent-hint />
        <v-text-field v-model.number="form.sort_order" type="number" min="0" max="9999" label="並び順"
          hint="同じ階層の中での表示順です。小さいほど上に出ます。" persistent-hint />
        <v-text-field v-model="form.note" label="備考（任意）" />
        <v-switch v-model="form.enabled" color="primary" density="compact"
          :label="form.enabled ? '所属に選べる' : '停止中'"
          hint="停止すると、配下の組織も所属に選べず、アプリの利用許可も届かなくなります。" persistent-hint />
      </div>
      <div class="form-actions">
        <v-btn v-if="form.id" variant="text" :disabled="loading" @click="startAdd()">追加に戻る</v-btn>
        <v-btn color="primary" :loading="loading" :disabled="!form.name?.trim()" @click="submit">
          {{ form.id ? '保存する' : '追加' }}</v-btn>
      </div>
      <p class="tip tip--compact mt-5">配下の組織や所属している利用者がいる組織は削除できません。
        使わなくなった組織は「停止中」にしてください。</p>
    </v-card>
  </div>

  <v-dialog :model-value="!!removing" max-width="560" :persistent="loading"
    @update:model-value="value => { if (!value) removing = undefined }">
    <v-card class="pa-6">
      <h2>組織を削除しますか？</h2>
      <p class="my-4">{{ removing?.path }}</p>
      <p>この組織に付けていたアプリの利用許可も外れます。この操作は取り消せません。</p>
      <v-card-actions class="dialog-actions">
        <v-btn :disabled="loading" @click="removing = undefined">キャンセル</v-btn>
        <v-btn color="error" :loading="loading" @click="applyRemoval">削除する</v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<style scoped>
.page-heading { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: var(--sp-5); }
.page-heading p { flex: 1 1 28rem; color: var(--ink-3); }
.heading-actions { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
/* 木を広く、編集欄は読みやすい幅に留める。狭い画面では縦に積む。 */
.org-layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(18rem, 26rem); align-items: start; gap: var(--sp-5); }
@media (max-width: 900px) { .org-layout { grid-template-columns: 1fr; } }

.tree { list-style: none; }
/* 段は字下げと縦の線で示す。線は1段につき1本で、親の開閉記号の真下に通す。 */
.tree-row { --indent: 24px; position: relative; display: flex; align-items: center; gap: var(--sp-1);
  min-height: var(--control-lg); padding-inline-start: calc(var(--depth) * var(--indent));
  border-radius: var(--radius-md); }
.tree-row:hover, .tree-row:focus-within { background: var(--surface-subtle); }
.tree-row.is-selected { background: var(--surface-accent); }
.guide { position: absolute; top: 0; bottom: 0; left: calc((var(--level) - 1) * var(--indent) + 12px);
  border-left: 1px solid var(--border-mid); }
.toggle, .leaf { display: inline-flex; flex: 0 0 24px; align-items: center; justify-content: center;
  width: 24px; height: 24px; color: var(--ink-3); }
.toggle { border-radius: var(--radius-sm); }
.toggle:hover { background: var(--surface-muted); color: var(--ink-1); }
.leaf::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--border-brand); }
.tree-main { display: flex; flex: 1; flex-wrap: wrap; align-items: center; gap: var(--sp-2); min-width: 0;
  padding: var(--sp-2) var(--sp-1); color: var(--ink-1); text-align: left; }
.tree-name { font-weight: var(--fw-bold); overflow-wrap: anywhere; }
.is-off .tree-name { color: var(--ink-3); }
.note { overflow-wrap: anywhere; }
/* 操作は行に乗ったときだけ出す。キーボードで入ったときも出す。触れる画面では常に出す。 */
.tree-actions { display: flex; flex: 0 0 auto; opacity: 0; }
.tree-row:hover .tree-actions, .tree-row:focus-within .tree-actions, .is-selected .tree-actions { opacity: 1; }
@media (hover: none) { .tree-actions { opacity: 1; } }

.form-fields { display: flex; flex-direction: column; gap: var(--sp-5); margin-top: var(--sp-5); }
.form-actions { display: flex; justify-content: flex-end; gap: var(--sp-2); margin-top: var(--sp-5); }
.dialog-actions { padding: 0; margin-top: var(--sp-6); gap: var(--sp-2); }
</style>
