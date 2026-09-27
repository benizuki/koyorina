<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { highlight } from '@/composables/useHighlight'
import { useClipboard } from '@/composables/useClipboard'
import { renderMarkdown } from '@/composables/useMarkdown'
import { useSources } from '@/composables/useSources'
const props = defineProps<{ projectId: string; projectName?: string; active: boolean; generating: boolean }>()
const { files, truncated, selected, text, loading, error, downloading, refresh, open, download } =
  useSources(props.projectId)
// 行番号を付ける。指摘が「main.py:12」の形で来るので、目で追えるようにする。
const lines = computed(() => text.value.split('\n'))
// 色付けは highlight.js。入力はエスケープされて返るので、そのまま描画してよい。
const colored = computed(() => highlight(selected.value, text.value))
// Markdownは読む形で出す。切り替えで元の文面も見られるようにする。
const markdown = computed(() => selected.value.toLowerCase().endsWith('.md'))
const raw = ref(false)
const article = computed(() => renderMarkdown(text.value))
// 表示中のファイルの中身をそのままコピーする。整形表示のMarkdownでも、元の文面を渡す。
const clipboard = useClipboard()
const copied = clipboard.copied
async function copy() {
  if (!await clipboard.copy(text.value)) error.value = clipboard.error.value
}
watch(selected, () => { raw.value = false; clipboard.reset() })

type Row = { path: string; name: string; depth: number; folder: boolean }
const closed = ref(new Set<string>())
// フォルダは既定で開く。数十ファイルなので、全部畳むと毎回開く手間だけが増える。
function toggle(path: string) {
  const next = new Set(closed.value)
  next.has(path) ? next.delete(path) : next.add(path)
  closed.value = next
}
const rows = computed<Row[]>(() => {
  const result: Row[] = []
  const seen = new Set<string>()
  for (const path of [...files.value].sort()) {
    const parts = path.split('/')
    let prefix = ''
    for (const [index, part] of parts.entries()) {
      const folder = index < parts.length - 1
      prefix = prefix ? `${prefix}/${part}` : part
      if (folder && seen.has(prefix)) continue
      if (folder) seen.add(prefix)
      // 閉じたフォルダの中は出さない。上の階層が閉じていれば、その先も出さない。
      if (parts.slice(0, index).some((_, depth) => closed.value.has(parts.slice(0, depth + 1).join('/')))) break
      result.push({ path: prefix, name: part, depth: index, folder })
    }
  }
  return result
})
onMounted(() => { if (props.active) refresh() })
watch(() => props.active, value => { if (value) refresh() })
// 生成が終わった時点で読み直す。古い中身を出したままにしない。
watch(() => props.generating, (now, before) => { if (before && !now && props.active) refresh() })
</script>

<template>
  <div class="browser">
    <div class="bar">
      <v-btn variant="text" size="small" prepend-icon="mdi-refresh" :loading="loading"
        @click="refresh">一覧を更新</v-btn>
      <span v-if="truncated" class="meta">表示は先頭200件までです（ダウンロードには全件入ります）。</span>
      <v-btn variant="outlined" size="small" prepend-icon="mdi-download-outline" class="ml-auto"
        :loading="downloading" :disabled="!files.length || generating"
        @click="download(projectName || `app-${projectId}`)">ZIPでダウンロード</v-btn>
    </div>
    <v-alert v-if="error" type="warning" class="mb-3" density="compact">{{ error }}</v-alert>
    <p v-if="!files.length" class="tip">まだファイルがありません。作成を依頼すると、ここに表示します。</p>
    <div v-else class="panes">
      <nav class="tree">
        <button v-for="row in rows" :key="row.path" class="row"
          :class="{ 'row--on': !row.folder && row.path === selected }"
          :style="{ paddingLeft: `calc(var(--sp-2) + ${row.depth} * var(--sp-4))` }"
          @click="row.folder ? toggle(row.path) : open(row.path)">
          <v-icon size="14" :icon="row.folder
            ? (closed.has(row.path) ? 'mdi-chevron-right' : 'mdi-chevron-down')
            : 'mdi-file-outline'" />
          <span>{{ row.name }}</span>
        </button>
      </nav>
      <div class="view">
        <p v-if="!selected" class="tip">左の一覧からファイルを選ぶと、中身を表示します。</p>
        <template v-else>
          <div class="head">
            <p class="path">{{ selected }}</p>
            <v-btn variant="text" size="x-small" :disabled="loading || !text"
              :prepend-icon="copied ? 'mdi-check' : 'mdi-content-copy'" @click="copy">
              {{ copied ? 'コピーしました' : '内容をコピー' }}</v-btn>
            <v-btn v-if="markdown" variant="text" size="x-small"
              :prepend-icon="raw ? 'mdi-text-box-outline' : 'mdi-code-tags'"
              @click="raw = !raw">{{ raw ? '整形して表示' : '元の文面' }}</v-btn>
          </div>
          <article v-if="markdown && !raw" class="markdown" v-html="article" />
          <div v-else class="code">
            <pre class="numbers">{{ lines.map((_, index) => index + 1).join('\n') }}</pre>
            <pre class="text"><code v-html="colored" /></pre>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.browser { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.bar { display: flex; gap: var(--sp-4); align-items: center; margin-bottom: var(--sp-2); }
.panes { display: grid; grid-template-columns: minmax(180px, 1fr) 3fr; gap: var(--sp-4);
  flex: 1; min-height: 0; }
.tree { overflow-y: auto; min-height: 0; border-right: 1px solid var(--border-default);
  padding-right: var(--sp-2); }
.row { display: flex; align-items: center; gap: var(--sp-1); width: 100%; text-align: left;
  padding: var(--sp-1) var(--sp-2); border-radius: var(--radius-sm); font-size: var(--fs-sm);
  color: var(--ink-2); white-space: nowrap; }
.row:hover { background: var(--surface-subtle); }
.row--on { background: var(--surface-accent); color: var(--ink-1); font-weight: var(--fw-medium); }
.view { display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
.head { display: flex; gap: var(--sp-3); align-items: center; margin-bottom: var(--sp-2); }
.path { font-size: var(--fs-xs); color: var(--ink-4); }
/* Markdownは文章として読む場所。行間と見出しの間隔だけ整える。 */
.markdown { flex: 1; min-height: 0; overflow: auto; padding: var(--sp-4);
  background: var(--surface-subtle); border-radius: var(--radius-md); font-size: var(--fs-sm);
  line-height: 1.8; color: var(--ink-2); }
.markdown :deep(h1) { font-size: var(--fs-lg); }
.markdown :deep(h2) { font-size: var(--fs-md); }
.markdown :deep(h3), .markdown :deep(h4) { font-size: var(--fs-sm); }
.markdown :deep(h1), .markdown :deep(h2), .markdown :deep(h3), .markdown :deep(h4) {
  color: var(--ink-1); margin: var(--sp-5) 0 var(--sp-2); }
.markdown :deep(p), .markdown :deep(ul), .markdown :deep(ol) { margin: 0 0 var(--sp-3); }
.markdown :deep(ul), .markdown :deep(ol) { padding-left: var(--sp-6); }
.markdown :deep(code) { font-family: var(--font-mono);
  font-size: var(--fs-xs); background: var(--surface-muted); border-radius: var(--radius-sm);
  padding: 0 var(--sp-1); }
.markdown :deep(pre) { background: var(--surface-sunken); border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: var(--sp-3); overflow-x: auto; margin: 0 0 var(--sp-3); }
.markdown :deep(pre code) { background: none; padding: 0; }
.markdown :deep(table) { border-collapse: collapse; margin: 0 0 var(--sp-3); }
.markdown :deep(th), .markdown :deep(td) { border: 1px solid var(--border-default);
  padding: var(--sp-1) var(--sp-3); text-align: left; }
.markdown :deep(th) { color: var(--ink-1); background: var(--surface-muted); }
.markdown :deep(blockquote) { border-left: 3px solid var(--border-brand); margin: 0 0 var(--sp-3);
  padding-left: var(--sp-4); color: var(--ink-3); }
.markdown :deep(a) { color: var(--text-brand); }
.markdown :deep(img) { max-width: 100%; }
.code { display: flex; gap: var(--sp-3); flex: 1; min-height: 0; overflow: auto;
  background: var(--surface-subtle); border-radius: var(--radius-md); padding: var(--sp-3); }
.numbers { color: var(--ink-muted); text-align: right; user-select: none; }
/* 色は配色トークンから取る。テーマを切り替えても浮かないようにする。 */
.text :deep(.hljs-keyword), .text :deep(.hljs-built_in), .text :deep(.hljs-literal),
.text :deep(.hljs-selector-tag) { color: var(--text-brand); }
.text :deep(.hljs-string), .text :deep(.hljs-attr), .text :deep(.hljs-attribute),
.text :deep(.hljs-selector-attr) { color: var(--text-warn); }
.text :deep(.hljs-number), .text :deep(.hljs-symbol), .text :deep(.hljs-meta) { color: var(--text-danger); }
.text :deep(.hljs-comment), .text :deep(.hljs-quote) { color: var(--ink-muted); font-style: italic; }
.text :deep(.hljs-title), .text :deep(.hljs-name), .text :deep(.hljs-section) { color: var(--ink-1);
  font-weight: var(--fw-medium); }
.text :deep(.hljs-tag) { color: var(--ink-3); }
.text { flex: 1; }
pre { font-family: var(--font-mono); font-size: var(--fs-xs);
  line-height: 1.6; margin: 0; white-space: pre; }
.meta { color: var(--ink-4); font-size: var(--fs-xs); }
</style>
