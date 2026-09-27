<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'

const files = defineModel<File[]>({ required: true })
const picker = ref<HTMLInputElement>()
const error = ref('')
const previews = ref<{ file: File; url: string }[]>([])
const imageTypes = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp'])
const suffixes: Record<string, string> = {
  'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/webp': '.webp',
}

function refreshPreviews(next: File[]) {
  for (const item of previews.value) URL.revokeObjectURL(item.url)
  previews.value = next.map(file => ({ file, url: URL.createObjectURL(file) }))
}
watch(files, refreshPreviews, { immediate: true })
onUnmounted(() => refreshPreviews([]))

function add(candidates: File[]) {
  error.value = ''
  const next = [...files.value]
  for (const source of candidates) {
    if (!imageTypes.has(source.type)) {
      error.value = 'PNG・JPEG・GIF・WebPの画像を選んでください。'
      continue
    }
    if (source.size > 4 * 1024 * 1024) {
      error.value = '画像は1件4MBまでです。'
      continue
    }
    if (next.length >= 4) {
      error.value = '参考画面は4件までです。'
      break
    }
    if (next.reduce((sum, file) => sum + file.size, 0) + source.size > 16 * 1024 * 1024) {
      error.value = '画像の合計は16MBまでです。'
      break
    }
    const suffix = suffixes[source.type]
    const name = `参考画面_${next.length + 1}_${crypto.randomUUID().slice(0, 8)}${suffix}`
    next.push(new File([source], name, { type: source.type, lastModified: Date.now() }))
  }
  files.value = next
}

function choose(event: Event) {
  const input = event.target as HTMLInputElement
  add(Array.from(input.files ?? []))
  input.value = ''
}

function paste(event: ClipboardEvent) {
  const direct = Array.from(event.clipboardData?.files ?? [])
  const items = direct.length ? [] : Array.from(event.clipboardData?.items ?? [])
    .map(item => item.kind === 'file' ? item.getAsFile() : null)
    .filter((file): file is File => !!file)
  const images = [...direct, ...items].filter(file => file.type.startsWith('image/'))
  if (!images.length) return
  event.preventDefault()
  add(images)
}

function remove(index: number) {
  files.value = files.value.filter((_, current) => current !== index)
}
</script>

<template>
  <section class="reference-images">
    <div>
      <h2>参考にしたい画面はありますか？</h2>
      <p>任意です。グラフの種類や配置を参考にします。画像内の数値や名称はサンプルとして扱います。</p>
    </div>
    <div class="paste-target" tabindex="0" role="button"
      aria-label="参考画像を選択、またはクリップボードから貼り付け"
      @paste="paste" @keydown.enter="picker?.click()">
      <v-icon icon="mdi-image-plus-outline" color="primary" />
      <span>ここを選択して画像を貼り付けるか、画像ファイルを選んでください。</span>
      <v-btn variant="outlined" prepend-icon="mdi-folder-image" @click.stop="picker?.click()">
        画像を選ぶ
      </v-btn>
      <input ref="picker" class="hidden-input" type="file" multiple
        accept="image/png,image/jpeg,image/gif,image/webp" @change="choose" />
    </div>
    <v-alert v-if="error" type="warning" variant="tonal" density="compact">{{ error }}</v-alert>
    <div v-if="previews.length" class="preview-grid" aria-label="参考画面のプレビュー">
      <figure v-for="(item, index) in previews" :key="item.file.name" class="preview-card">
        <img :src="item.url" :alt="`参考画面${index + 1}`" />
        <figcaption>
          <span>参考画面{{ index + 1 }}</span>
          <v-btn icon="mdi-close" variant="text" size="small"
            :aria-label="`参考画面${index + 1}を外す`" @click="remove(index)" />
        </figcaption>
      </figure>
    </div>
    <p class="meta">PNG・JPEG・GIF・WebP、1件4MB・最大4件。画像はアプリ生成時の参考資料として使います。</p>
  </section>
</template>

<style scoped>
.reference-images { display: flex; flex-direction: column; gap: var(--sp-3); }
.reference-images p { color: var(--ink-3); }
.paste-target { display: flex; align-items: center; gap: var(--sp-3); padding: var(--sp-4);
  border: 1px dashed var(--border-brand); border-radius: var(--radius-md); background: var(--surface-subtle); }
.paste-target span { flex: 1 1 auto; }
.paste-target:focus-visible { outline: 2px solid var(--border-brand); outline-offset: 2px; }
.hidden-input { display: none; }
.preview-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(var(--forge-card-min), 1fr)); gap: var(--sp-3); }
.preview-card { margin: 0; padding: var(--sp-2); border: 1px solid var(--border-default);
  border-radius: var(--radius-md); background: var(--surface-sunken); }
.preview-card img { display: block; width: 100%; height: 10rem; object-fit: contain; }
.preview-card figcaption { display: flex; align-items: center; justify-content: space-between;
  gap: var(--sp-2); color: var(--ink-3); font-size: var(--fs-sm); }
.meta { font-size: var(--fs-xs); }
@media (max-width: 600px) { .paste-target { align-items: stretch; flex-direction: column; } }
</style>
