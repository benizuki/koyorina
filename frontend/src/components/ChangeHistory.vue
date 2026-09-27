<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useCodeHistory } from '@/composables/useCodeHistory'

const props = defineProps<{ projectId: string; active: boolean; readOnly: boolean
                            canAdminister: boolean }>()
const changes = useCodeHistory(props.projectId)
const picked = ref<string>()
const file = ref<string>()
const restoring = ref<{ commit: string; summary: string }>()
const clearing = ref(false)

// 拡張子で見分けが付くと、目的のファイルを探す時間が縮む。色は付けない。
// 色は「追加・変更・削除」に使う。種類にも使うと、どちらの意味か読めなくなる。
const ICONS: Record<string, string> = {
  py: 'mdi-language-python', ts: 'mdi-language-typescript', js: 'mdi-language-javascript',
  mjs: 'mdi-language-javascript', vue: 'mdi-vuejs', css: 'mdi-language-css3',
  html: 'mdi-language-html5', json: 'mdi-code-json', md: 'mdi-language-markdown-outline',
  sql: 'mdi-database-outline', yaml: 'mdi-file-cog-outline', yml: 'mdi-file-cog-outline',
  toml: 'mdi-file-cog-outline', ini: 'mdi-file-cog-outline', txt: 'mdi-file-document-outline',
}
const icon = (path: string) => ICONS[path.split('.').pop()?.toLowerCase() ?? ''] ?? 'mdi-file-outline'
const folder = (path: string) => path.includes('/') ? path.slice(0, path.lastIndexOf('/') + 1) : ''
const name = (path: string) => path.slice(path.lastIndexOf('/') + 1)
const CHANGES: Record<string, string> = { A: '追加', M: '変更', D: '削除', R: '改名', C: '複製' }

// 差分はファイル単位で読む。全部を一続きで出すと、どこを見ているか見失う。
const perFile = computed(() => {
  const text = changes.diff.value?.text ?? ''
  const sections = new Map<string, string>()
  let path = '', buffer: string[] = []
  for (const line of text.split('\n')) {
    if (line.startsWith('diff --git ')) {
      if (path) sections.set(path, buffer.join('\n'))
      // "diff --git a/x b/x" の後ろ側を採る。前後で同じか、改名なら移動先。
      path = line.split(' b/').slice(1).join(' b/') || line
      buffer = []
      continue
    }
    if (path) buffer.push(line)
  }
  if (path) sections.set(path, buffer.join('\n'))
  return sections
})
// 行頭の記号で色を分ける。読むのは「何が増えて何が減ったか」なので、そこだけ立てる。
const MAX_LINES = 2000
const lines = computed(() => {
  const text = file.value ? perFile.value.get(file.value) ?? '' : ''
  if (!text) return []
  return text.split('\n').slice(0, MAX_LINES).map(text => ({
    text,
    kind: text.startsWith('@@') ? 'hunk'
      : text.startsWith('+++') || text.startsWith('---') ? 'meta'
      : text.startsWith('+') ? 'add' : text.startsWith('-') ? 'del' : 'same',
  }))
})
const clipped = computed(() =>
  (file.value ? perFile.value.get(file.value) ?? '' : '').split('\n').length > MAX_LINES)

async function select(commit: string) {
  picked.value = picked.value === commit ? undefined : commit
  file.value = undefined
  if (picked.value) await changes.show(commit)
}
async function confirmRestore() {
  const target = restoring.value
  restoring.value = undefined
  if (target && await changes.restore(target.commit)) { picked.value = undefined; file.value = undefined }
}
async function confirmClear() {
  clearing.value = false
  if (await changes.discard()) { picked.value = undefined; file.value = undefined }
}
watch(() => props.active, on => { if (on) changes.refresh() })
onMounted(() => { if (props.active) changes.refresh() })
defineExpose({ refresh: changes.refresh })
</script>

<template>
  <div class="history">
    <v-alert v-if="changes.error.value" type="warning" density="compact" class="mb-3">
      {{ changes.error.value }}</v-alert>
    <p v-if="!changes.entries.value.length" class="tip">
      <v-icon icon="mdi-source-commit" />
      <span>まだ記録がありません。次の生成から、そのとき何が変わったかを残します。</span>
    </p>

    <template v-else>
      <!-- 上から新しい順。1点＝生成1回。選ぶとその回で変わったファイルが下に開く。 -->
      <div class="graph">
        <template v-for="(item, index) in changes.entries.value" :key="item.commit">
          <button type="button" class="node" :class="{ 'node--open': picked === item.commit }"
            @click="select(item.commit)">
            <!-- 最初と最後は線を半分で止める。突き抜けると、続きがあるように見える。 -->
            <span class="rail" :class="{ 'rail--first': index === 0,
              'rail--last': index === changes.entries.value.length - 1 && picked !== item.commit }">
              <span class="dot" /></span>
            <span class="node-body">
              <span class="summary">{{ item.summary }}</span>
              <span class="meta">{{ item.author }}　{{
                new Date(item.at).toLocaleString('ja-JP', { dateStyle: 'short', timeStyle: 'short' })
              }}　{{ item.commit.slice(0, 7) }}</span>
            </span>
            <v-icon class="caret" :icon="picked === item.commit ? 'mdi-menu-down' : 'mdi-menu-right'" />
          </button>

          <div v-if="picked === item.commit" class="branch">
            <div v-if="changes.loading.value && !changes.diff.value" class="rail-row">
              <span class="rail" /><span class="meta">読み込んでいます…</span></div>
            <button v-for="entry in changes.diff.value?.files ?? []" :key="entry.path"
              type="button" class="file" :class="{ 'file--open': file === entry.path }"
              @click="file = entry.path">
              <span class="rail" />
              <v-icon :icon="icon(entry.path)" size="16" class="kind" />
              <span class="path"><span class="dir">{{ folder(entry.path) }}</span><span
                class="leaf">{{ name(entry.path) }}</span></span>
              <!-- 記号と文字の両方で示す。色だけだと読み取れない場合がある。 -->
              <span class="mark" :class="`mark--${entry.change}`"
                :title="CHANGES[entry.change] ?? entry.change">{{ entry.change }}</span>
            </button>
            <div class="rail-row node-actions">
              <span class="rail" :class="{ 'rail--last': index === changes.entries.value.length - 1 }" />
              <v-btn variant="outlined" size="small" :disabled="readOnly || changes.loading.value"
                @click="restoring = { commit: item.commit, summary: item.summary }">
                この時点へ戻す</v-btn>
              <span v-if="changes.diff.value?.truncated" class="meta">
                差分が大きいため、途中までを表示しています。</span>
            </div>
          </div>
        </template>
        <div v-if="canAdminister" class="tail">
          <v-btn variant="outlined" size="small" color="error"
            :disabled="changes.loading.value" @click="clearing = true">変更履歴を削除</v-btn>
        </div>
      </div>

      <div v-if="file" class="diff">
        <div class="diff-bar">
          <v-icon :icon="icon(file)" size="16" />
          <span class="path"><span class="dir">{{ folder(file) }}</span><span
            class="leaf">{{ name(file) }}</span></span>
          <v-spacer />
          <v-btn icon="mdi-close" variant="text" size="x-small" @click="file = undefined" />
        </div>
        <pre v-if="lines.length" class="diff-body"><span v-for="(row, index) in lines" :key="index"
          class="line" :class="`line--${row.kind}`">{{ row.text || ' ' }}</span></pre>
        <p v-else class="tip tip--compact">このファイルの差分は表示できません。</p>
        <p v-if="clipped" class="meta pa-2">この先は長いため省略しました。</p>
      </div>
    </template>

    <v-dialog :model-value="!!restoring" max-width="560"
      @update:model-value="value => { if (!value) restoring = undefined }">
      <v-card class="pa-6">
        <h2>この時点へ戻しますか？</h2>
        <p class="my-4">作業場所の中身を「{{ restoring?.summary }}」の時点に戻します。
          <strong>戻す前の状態も履歴に残す</strong>ので、あとから戻し直せます。
          プレビューは次に起動したときから新しい内容になります。</p>
        <v-card-actions>
          <v-btn variant="outlined" @click="restoring = undefined">キャンセル</v-btn>
          <v-btn color="primary" :loading="changes.loading.value" @click="confirmRestore">戻す</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <v-dialog v-model="clearing" max-width="560">
      <v-card class="pa-6">
        <h2>変更履歴を削除しますか？</h2>
        <p class="my-4">これまでの記録と差分がすべて消えます。<strong>元に戻せません。</strong>
          いまの作業場所の中身は消えませんが、過去のどの時点へも戻せなくなります。
          次の生成から、また新しく記録しはじめます。</p>
        <v-card-actions>
          <v-btn variant="outlined" @click="clearing = false">キャンセル</v-btn>
          <v-btn color="error" :loading="changes.loading.value" @click="confirmClear">削除する</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<style scoped>
.history { display: flex; flex-direction: column; height: 100%; min-height: 0; gap: var(--sp-3); }
.graph { flex: 1; min-height: 0; overflow-y: auto; }

/* 左端の1本線が「同じ枝の続き」を表す。点が生成1回。VSCodeのグラフと同じ読み方。 */
.rail { position: relative; flex: none; width: 20px; align-self: stretch; }
.rail::before { content: ''; position: absolute; left: 9px; top: 0; bottom: 0;
  width: 2px; background: var(--border-mid); }
.rail--first::before { top: 50%; }
.rail--last::before { bottom: 50%; }
.dot { position: absolute; left: 4px; top: 50%; width: 12px; height: 12px; margin-top: -6px;
  border-radius: 50%; background: var(--surface-card); border: 2px solid var(--border-mid); }
.node--open .dot { background: var(--surface-accent-solid); border-color: var(--surface-accent-solid);
  box-shadow: 0 0 0 3px var(--surface-accent); }

.node { display: flex; align-items: stretch; gap: var(--sp-2); width: 100%; text-align: left;
  padding: var(--sp-2) var(--sp-2) var(--sp-2) 0; border-radius: var(--radius-sm); }
.node:hover { background: var(--surface-muted); }
.node--open { background: var(--surface-accent); }
.node-body { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1; }
.summary { font-size: var(--fs-sm); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.node--open .summary { font-weight: var(--fw-medium); color: var(--text-brand); }
.meta { color: var(--ink-4); font-size: var(--fs-xs); }
.caret { align-self: center; color: var(--ink-4); }

.branch { display: flex; flex-direction: column; }
.rail-row { display: flex; align-items: center; gap: var(--sp-2); min-height: 28px; }
.node-actions { padding-bottom: var(--sp-2); flex-wrap: wrap; }
.file { display: flex; align-items: center; gap: var(--sp-2); width: 100%; text-align: left;
  padding-right: var(--sp-2); border-radius: var(--radius-sm); font-size: var(--fs-sm);
  min-height: 28px; }
.file:hover { background: var(--surface-muted); }
.file--open { background: var(--surface-muted); font-weight: var(--fw-medium); }
.kind { color: var(--ink-4); flex: none; }
/* 狭いときに削るのはフォルダ側。ファイル名が消えると、何の差分か分からなくなる。 */
.path { display: flex; min-width: 0; flex: 1; white-space: nowrap; }
.dir { color: var(--ink-4); overflow: hidden; text-overflow: ellipsis; }
.leaf { flex: none; }
/* 追加・変更・削除を文字でも示す。色だけだと読み取れない場合がある。 */
.mark { flex: none; width: 1.2rem; text-align: center; font-weight: var(--fw-bold);
  font-size: var(--fs-xs); }
.mark--A { color: var(--text-brand); }
.mark--D { color: var(--text-danger); }
.mark--M, .mark--R, .mark--C { color: var(--ink-3); }

.tail { padding: var(--sp-3) 0 0 20px; }

.diff { display: flex; flex-direction: column; min-height: 0; flex: 0 1 45%;
  border: 1px solid var(--border-subtle); border-radius: var(--radius-md);
  background: var(--surface-sunken); overflow: hidden; }
.diff-bar { display: flex; align-items: center; gap: var(--sp-2); font-size: var(--fs-xs);
  padding: var(--sp-1) var(--sp-1) var(--sp-1) var(--sp-3);
  border-bottom: 1px solid var(--border-subtle); }
/* 差分は行頭の +/- で読む。折り返すと記号の位置が崩れるので、横に流す。 */
.diff-body { flex: 1; min-height: 0; overflow: auto; font-size: var(--fs-xs);
  display: flex; flex-direction: column; padding: var(--sp-2) 0; }
.line { white-space: pre; padding: 0 var(--sp-3); color: var(--ink-2); }
.line--add { background: var(--surface-accent); color: var(--text-brand); }
.line--del { background: var(--surface-danger); color: var(--text-danger); }
.line--hunk { color: var(--ink-4); background: var(--surface-muted); }
.line--meta { color: var(--ink-4); }
</style>
