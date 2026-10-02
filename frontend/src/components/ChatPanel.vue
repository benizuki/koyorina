<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import GenerationProgress from './GenerationProgress.vue'
import { useAttachments } from '@/composables/useAttachments'
import { renderMarkdown } from '@/composables/useMarkdown'
import { useModels } from '@/composables/useModels'
import { stored } from '@/composables/useStored'
import { useVoice } from '@/composables/useVoice'
import type { CodexStatus, GenerationJob, Project } from '@/types'
const props = defineProps<{
  project: Project; jobs: GenerationJob[]; loading: boolean; enabled: boolean
  status?: CodexStatus; active: boolean
}>()
type Choice = { model: string; effort: string; provider: string; chat_id: string }
const emit = defineEmits<{ generate: [choice: Choice]; instruct: [text: string, choice: Choice];
  cancel: [jobId: string]; revalidate: [jobId: string]; connect: [] }>()
const text = ref('')
const newChatId = () => crypto.randomUUID()
const currentChat = ref('')
const { models, loaded: modelsLoaded, refresh: refreshModels } = useModels()
// 前回選んだものを覚えておく。毎回選び直すのは手間でしかない。
const model = stored('model', ''), effort = stored('effort', 'medium')
const chosen = computed(() => models.value.find(m => m.id === model.value))
const efforts = computed(() => chosen.value?.efforts ?? [])
const choices = computed(() => models.value.map(m => ({ title: m.label, value: m.id })))
// 選択肢が届いたら1つ選んでおく。未選択の行を見せても選ぶ手間が増えるだけ。
watch([models, modelsLoaded], ([list, loaded]) => {
  // 初回取得前の空配列で保存値を消さない。取得失敗時も、次回復旧に備えて残す。
  if (!loaded || !list.length) return
  // 覚えていたモデルが今も選べるならそれを使う。無くなっていたときだけ選び直す。
  if (!list.some(m => m.id === model.value)) model.value = (list.find(m => m.is_default) ?? list[0])?.id ?? ''
}, { immediate: true })
// 一覧取得後にも検証する。モデル名が保存値のままならmodel自体は変化しないため。
watch([model, efforts], () => {
  if (!efforts.value.length) return
  if (!efforts.value.includes(effort.value)) {
    effort.value = chosen.value?.default_effort || efforts.value[0]
  }
})
// 添付と音声。どちらも依頼を送る前の入力を助けるもので、送信自体は変えない。
const files = useAttachments(props.project.id)
const voice = useVoice()
const picker = ref<HTMLInputElement>()
const pasteNotice = ref('')
const imagePreviews = ref<Record<string, string>>({})
const imageSuffix: Record<string, string> = {
  'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/webp': '.webp',
}
function rememberPreview(name: string, file: File) {
  if (!file.type.startsWith('image/')) return
  const before = imagePreviews.value[name]
  if (before) URL.revokeObjectURL(before)
  imagePreviews.value = { ...imagePreviews.value, [name]: URL.createObjectURL(file) }
}
async function addFile(file: File) {
  const added = await files.add(file)
  if (added) rememberPreview(added.name, file)
  return !!added
}
async function attach(event: Event) {
  const input = event.target as HTMLInputElement
  for (const file of Array.from(input.files ?? [])) await addFile(file)
  input.value = ''
}
// 長い文章（エラーの全文やログ）は、依頼の欄に入れずテキストの添付にする。Codex と同じ考え方。
// 欄は2000字で切れるので、そのまま貼ると末尾（たいてい肝心のところ）が黙って落ちる。
const LONG_PASTE_CHARS = 1000, LONG_PASTE_LINES = 20
function isLongText(value: string) {
  return value.length > LONG_PASTE_CHARS || value.split('\n').length > LONG_PASTE_LINES
}
const stamp = () => new Date().toISOString().replace(/[-:TZ.]/g, '').slice(0, 17)
async function pasteLongText(value: string) {
  const name = `貼り付けテキスト_${stamp()}_${crypto.randomUUID().slice(0, 8)}.txt`
  pasteNotice.value = '長い文章をテキストファイルとして添付しています。'
  if (await addFile(new File([value], name, { type: 'text/plain', lastModified: Date.now() }))) {
    const lines = value.split('\n').length
    pasteNotice.value = `長い文章（${value.length.toLocaleString()}字・${lines}行）を「${name}」として添付しました。`
      + '依頼の欄には、してほしいこと（例：このエラーを直して）を書いてください。'
  } else pasteNotice.value = ''
}
async function pasteImages(event: ClipboardEvent) {
  const direct = Array.from(event.clipboardData?.files ?? [])
  const fromItems = direct.length ? [] : Array.from(event.clipboardData?.items ?? [])
    .map(item => item.kind === 'file' ? item.getAsFile() : null).filter((file): file is File => !!file)
  const images = [...direct, ...fromItems].filter(file => file.type.startsWith('image/'))
  const pasted = images.length ? '' : event.clipboardData?.getData('text/plain') ?? ''
  if (pasted && isLongText(pasted)) {
    event.preventDefault()
    if (props.active || files.loading.value) return
    await pasteLongText(pasted)
    return
  }
  if (!images.length) return
  event.preventDefault()
  pasteNotice.value = '画像を添付しています。生成Podの起動中は少し時間がかかります。'
  if (props.active || files.loading.value) return
  let added = 0
  for (const [index, image] of images.entries()) {
    const suffix = imageSuffix[image.type]
    if (!suffix) {
      files.error.value = '貼り付けられる画像はPNG・JPEG・GIF・WebPです。'
      continue
    }
    const name = `貼り付け画像_${stamp()}_${index + 1}_${crypto.randomUUID().slice(0, 8)}${suffix}`
    const named = new File([image], name, { type: image.type, lastModified: Date.now() })
    if (await addFile(named)) added += 1
  }
  if (added) pasteNotice.value = `${added}件の画像を添付しました。この依頼と一緒に送信されます。`
}
async function removeAttachment(name: string) {
  await files.remove(name)
  const url = imagePreviews.value[name]
  if (url) URL.revokeObjectURL(url)
  const { [name]: _, ...remaining } = imagePreviews.value
  imagePreviews.value = remaining
}
async function dictate() {
  if (voice.recording.value) {
    const said = await voice.stop()
    if (said) text.value = text.value ? `${text.value} ${said}` : said
  } else await voice.start()
}
const size = (bytes: number) => bytes < 1024 * 1024
  ? `${Math.max(1, Math.round(bytes / 1024))}KB` : `${(bytes / 1024 / 1024).toFixed(1)}MB`
const thread = ref<HTMLElement>()
// 古い順に並べる。会話として上から読めるようにする。
const chats = computed(() => {
  const groups = new Map<string, GenerationJob[]>()
  for (const job of [...props.jobs].reverse()) {
    const group = groups.get(job.chat_id) ?? []
    group.push(job); groups.set(job.chat_id, group)
  }
  return [...groups.entries()].reverse().map(([id, jobs]) => {
    const first = jobs[0]
    return { value: id, title: first.instruction?.trim().slice(0, 32) || '初回開発' }
  })
})
const chatChoices = computed(() => currentChat.value && !chats.value.some(chat => chat.value === currentChat.value)
  ? [{ value: currentChat.value, title: '新しいチャット' }, ...chats.value] : chats.value)
const exchanges = computed(() => [...props.jobs]
  .filter(job => job.chat_id === currentChat.value).reverse())
const generated = computed(() => props.jobs.some(j => j.status === 'generated'))
// ChatGPTの接続が要るのはCodexを使うときだけ。Geminiは会社のGCPで動く。
const provider = computed(() => chosen.value?.provider || props.status?.generator || 'codex')
const needsCodex = computed(() => provider.value === 'codex')
const ready = computed(() => props.enabled && props.project.status === 'approved'
  && (!needsCodex.value || props.status?.status === 'connected'))
const labels = { starting: '環境を準備中', generating: '作成中', generated: '完了', failed: '失敗' }
const running = computed(() => props.jobs.find(j => ['starting', 'generating'].includes(j.status)))
// 失敗・停止した最新の生成は、作り直さずに作業場所の検査をやり直せる。
// 作業場所には最新の生成の中身しか無いので、古い生成は対象にしない。
// 検査で落ちたあとに再生成を止めると最新は「停止」になるので、失敗の理由では絞らない。
const latestJobId = computed(() => [...props.jobs]
  .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))[0]?.id)
const canRevalidate = (job: GenerationJob) => job.status === 'failed' && job.id === latestJobId.value
  && job.source_type === 'managed_codex'
function startNewChat() { currentChat.value = newChatId(); text.value = '' }
// 変換確定のEnterで送らない。IME中はkeydownのisComposingが立つ。
function submitKey(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode === 229) return
  event.preventDefault()
  send()
}
function send() {
  if (props.active || files.loading.value || !ready.value) return
  const requestModel = chosen.value?.provider === 'antigravity'
    ? chosen.value.id.replace(/^antigravity-/, '') : model.value
  const choice: Choice = { model: requestModel, effort: efforts.value.length ? effort.value : '',
                           provider: chosen.value?.provider ?? '', chat_id: currentChat.value || newChatId() }
  if (generated.value) {
    if (!text.value.trim()) return
    emit('instruct', text.value.trim(), choice)
  } else emit('generate', choice)
  text.value = ''
}
onMounted(() => { refreshModels(props.project.tenant_id); files.refresh() })
watch(() => props.jobs, jobs => {
  const active = jobs.find(job => ['starting', 'generating'].includes(job.status))
  if (active) currentChat.value = active.chat_id
  else if (!currentChat.value) currentChat.value = jobs[0]?.chat_id ?? newChatId()
}, { immediate: true, deep: true })
watch(() => props.active, (now, before) => { if (before && !now) files.refresh() })
watch(() => files.items.value.map(file => file.name), names => {
  const present = new Set(names)
  const remaining: Record<string, string> = {}
  for (const [name, url] of Object.entries(imagePreviews.value)) {
    if (present.has(name)) remaining[name] = url
    else URL.revokeObjectURL(url)
  }
  imagePreviews.value = remaining
})
watch(() => props.jobs.length, async () => {
  await nextTick()
  thread.value?.scrollTo({ top: thread.value.scrollHeight, behavior: 'smooth' })
})
onUnmounted(() => Object.values(imagePreviews.value).forEach(url => URL.revokeObjectURL(url)))
</script>

<template>
  <div class="chat">
    <div class="chat-toolbar">
      <v-select v-if="chats.length" v-model="currentChat" :items="chatChoices" label="開発チャット"
        density="compact" hide-details class="chat-picker" />
      <span v-else class="meta">新しい開発チャット</span>
      <v-btn v-if="generated" variant="outlined" size="small" prepend-icon="mdi-message-plus-outline"
        :disabled="active" @click="startNewChat">新しいチャット</v-btn>
    </div>
    <div ref="thread" class="thread">
      <p v-if="!exchanges.length" class="tip">
        {{ generated ? '新しい改良内容を入力してください。このチャット内に依頼と結果をまとめます。'
          : '承認した仕様からアプリを作ります。下の「作成を依頼」で始めてください。' }}
      </p>
      <div v-for="job in exchanges" :key="job.id" class="exchange">
        <!-- 自分が出した依頼だと一目で分かるようにする。この下が、それに対する応答。 -->
        <p class="who"><v-icon size="14" icon="mdi-account-outline" /><span>依頼</span></p>
        <p v-if="job.instruction" class="bubble bubble--mine">{{ job.instruction }}</p>
        <p v-else class="bubble bubble--mine">承認した仕様（第{{ job.revision }}版）からアプリを作成</p>
        <!-- この依頼に添えた資料。次の依頼には持ち越さない。 -->
        <div v-if="job.attachments?.length" class="attached">
          <span v-for="name in job.attachments" :key="name" class="chip chip--sm">
            <v-icon size="12" icon="mdi-paperclip" />{{ name }}
          </span>
        </div>
        <div class="reply">
          <span class="chip chip--pill"
            :class="job.status === 'failed' ? 'chip--danger' : job.status === 'generated' ? 'chip--brand' : 'chip--warn'">
            {{ labels[job.status] }}
          </span>
          <span class="meta">{{ new Date(job.created_at).toLocaleString('ja-JP') }}</span>
          <span class="meta">{{ job.source_type === 'local_codex' ? '手元のCodexアプリ' : 'AppGen' }}</span>
        </div>
        <!-- 生成AIの最後の報告。Codex や Claude の画面と同じく、作業の締めくくりとして読める形で出す。
             本文は実行環境で伏せ字にしてあり、Markdownは HTML を通さない描画器で組み立てる。 -->
        <template v-if="job.summary || job.next_steps?.length">
          <p class="who who--ai"><v-icon size="14" icon="mdi-robot-outline" /><span>AppGenの報告</span></p>
          <div class="bubble bubble--ai">
            <div v-if="job.summary" class="report" v-html="renderMarkdown(job.summary)" />
            <template v-if="job.next_steps?.length">
              <p class="next-title">次に頼めること</p>
              <ul class="next"><li v-for="step in job.next_steps" :key="step">{{ step }}</li></ul>
            </template>
          </div>
        </template>
        <p v-if="job.error" class="tip tip--warn">{{ job.error }}</p>
        <div v-if="canRevalidate(job) && enabled" class="revalidate">
          <v-btn variant="tonal" color="primary" size="small" prepend-icon="mdi-shield-refresh-outline"
            :loading="loading" :disabled="loading || !!running" @click="emit('revalidate', job.id)">
            作り直さずに再検査して完了にする</v-btn>
          <span class="meta">書けているファイルをもう一度検査します。通れば、生成し直さずに完了になります。</span>
        </div>
        <GenerationProgress :project-id="project.id"
          :job-id="job.id" :active="['starting', 'generating'].includes(job.status)" />
      </div>
    </div>
    <div class="composer" @paste.capture="pasteImages">
      <p v-if="project.status !== 'approved'" class="tip tip--warn">
        先に仕様タブで内容を確認し、承認してください。
      </p>
      <p v-else-if="!enabled" class="tip tip--warn">AppGenの実行環境が未接続です。</p>
      <template v-else-if="needsCodex && status && status.status !== 'connected'">
        <p class="tip tip--warn">
          ご本人のCodexアカウントへのログインが必要です。Geminiのモデルを選べば、接続なしで作成できます。
        </p>
        <v-btn variant="outlined" size="small" @click="emit('connect')">Codexの接続へ</v-btn>
      </template>
      <div v-if="!generated" class="paste-target" tabindex="0"
        aria-label="クリップボードの画像を貼り付ける"
        :class="{ 'paste-target--disabled': active || files.loading.value }">
        <v-icon icon="mdi-image-plus-outline" />
        <span>ここを選んで画像を貼り付けるか、クリップボタンから資料を選べます。</span>
      </div>
      <v-textarea v-if="generated" v-model="text" rows="2" auto-grow variant="outlined"
        density="comfortable" hide-details maxlength="2000" :disabled="loading || active || !ready"
        placeholder="例：一覧に日付の絞り込みを足して、金額を右寄せにして"
        @keydown.enter.exact="submitKey" @keydown.enter.shift.exact.stop />
      <p v-if="generated" class="meta input-help">Enterで送信 ／ Shift＋Enterで改行 ／ 画像と長い文章（エラーの全文など）は貼り付けると添付になります</p>
      <p v-else class="meta input-help">PNG・JPEG・GIF・WebP、1件4MBまで。添付画像は初回生成の参考資料になります。</p>
      <v-alert v-if="pasteNotice" type="success" density="compact" closable class="my-2"
        @click:close="pasteNotice = ''">{{ pasteNotice }}</v-alert>
      <v-alert v-if="files.error.value || voice.error.value" type="warning" density="compact"
        class="my-2">{{ files.error.value || voice.error.value }}</v-alert>
      <div v-if="Object.keys(imagePreviews).length" class="image-previews" aria-label="貼り付けた画像">
        <figure v-for="(url, name) in imagePreviews" :key="name" class="image-preview">
          <img :src="url" :alt="`${name} のプレビュー`" />
          <figcaption>{{ name }}</figcaption>
        </figure>
      </div>
      <div v-if="files.items.value.length" class="attached">
        <span v-for="file in files.items.value" :key="file.name" class="chip chip--sm">
          <v-icon v-if="/\.(png|jpe?g|gif|webp)$/i.test(file.name)" size="12" icon="mdi-image-outline" />
          {{ file.name }}（{{ size(file.bytes) }}）
          <v-btn variant="text" size="x-small" icon="mdi-close" :disabled="files.loading.value || active"
            :aria-label="`${file.name} を外す`" @click="removeAttachment(file.name)" />
        </span>
      </div>
      <div class="input-row">
        <div class="tools">
          <input ref="picker" type="file" class="hidden-input" multiple :disabled="active || files.loading.value"
            accept="image/png,image/jpeg,image/gif,image/webp,application/pdf,.txt,.md,.csv,.tsv,.json"
            @change="attach" />
          <v-btn variant="text" size="small" icon="mdi-paperclip" title="資料を添える"
            aria-label="資料を添える" :loading="files.loading.value" :disabled="active || files.loading.value"
            @click="picker?.click()" />
          <v-btn variant="text" size="small" :color="voice.recording.value ? 'error' : undefined"
            :icon="voice.recording.value ? 'mdi-stop-circle-outline' : 'mdi-microphone-outline'"
            :title="voice.recording.value ? '録音を終えて文字にする' : '話して入力'"
            :aria-label="voice.recording.value ? '録音を終えて文字にする' : '話して入力'"
            :loading="voice.busy.value" :disabled="active" @click="dictate" />
          <!-- 録音中だけ経過を出す。90秒で自動的に止まるため、目安が要る。 -->
          <span v-if="voice.recording.value" class="meta">{{ voice.seconds.value }}秒</span>
        </div>
        <div class="center">
          <v-select v-model="model" :items="choices" density="compact" hide-details
            class="model" aria-label="モデル" :disabled="loading || active" />
          <v-select v-if="efforts.length" v-model="effort"
            :items="efforts.map(e => ({ title: e, value: e }))" density="compact" hide-details
            class="effort" aria-label="考える深さ" :disabled="loading || active" />
        </div>
        <div class="send">
          <v-btn v-if="running" color="error" variant="outlined" :loading="loading"
            prepend-icon="mdi-stop-circle-outline" @click="emit('cancel', running.id)">生成を止める</v-btn>
          <v-btn v-else color="primary" :loading="loading || files.loading.value"
            :disabled="!ready || files.loading.value || (generated && !text.trim())"
            :prepend-icon="generated ? 'mdi-send-outline' : 'mdi-play-outline'" @click="send">
            {{ generated ? '変更を依頼' : '作成を依頼' }}
          </v-btn>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chat { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.chat-toolbar { display: flex; align-items: center; justify-content: space-between; gap: var(--sp-3);
  padding-bottom: var(--sp-3); }
.chat-picker { max-width: 320px; }
.thread { flex: 1; overflow-y: auto; min-height: 0; padding-right: var(--sp-2); }
.exchange { margin-bottom: var(--sp-6); }
.who { display: flex; gap: var(--sp-1); align-items: center; margin-bottom: var(--sp-1);
  color: var(--text-brand); font-size: var(--fs-xs); font-weight: var(--fw-bold); }
.bubble { border-radius: var(--radius-md); padding: var(--sp-3) var(--sp-4); white-space: pre-wrap;
  font-size: var(--fs-sm); }
/* 依頼は、色の濃さだけでなく枠と左の帯でも示す。淡い面だけだと地に沈んで、
   どこからどこまでが自分の依頼か読み取れない。 */
.bubble--mine { background: var(--surface-accent); color: var(--ink-1);
  border: 1px solid var(--border-brand);
  box-shadow: inset 3px 0 var(--surface-accent-solid); padding-left: var(--sp-5); }
/* 報告は依頼と色を分ける。依頼＝ブランドの帯、報告＝落ち着いた面。 */
.who--ai { color: var(--ink-3); margin-top: var(--sp-3); }
.bubble--ai { background: var(--surface-subtle); color: var(--ink-2); border: 1px solid var(--border-default);
  white-space: normal; line-height: 1.8; overflow-wrap: anywhere; }
.report :deep(p), .report :deep(ul), .report :deep(ol) { margin: 0 0 var(--sp-2); }
.report :deep(ul), .report :deep(ol), .next { padding-left: var(--sp-5); }
.report :deep(h1), .report :deep(h2), .report :deep(h3), .report :deep(h4) { font-size: var(--fs-sm);
  color: var(--ink-1); margin: var(--sp-3) 0 var(--sp-1); }
.report :deep(code) { font-family: var(--font-mono); font-size: var(--fs-xs); background: var(--surface-muted);
  border-radius: var(--radius-sm); padding: 0 var(--sp-1); }
.report :deep(> :last-child) { margin-bottom: 0; }
.next-title { margin: var(--sp-3) 0 var(--sp-1); color: var(--ink-1); font-weight: var(--fw-medium); }
.next { margin: 0; }
.reply { display: flex; gap: var(--sp-3); align-items: center; flex-wrap: wrap; margin: var(--sp-3) 0; }
.composer { border-top: 1px solid var(--border-default); padding-top: var(--sp-4); margin-top: var(--sp-4); }
.paste-target { display: flex; gap: var(--sp-2); align-items: center; min-height: var(--control-lg);
  padding: var(--sp-3); border: 1px dashed var(--border-brand); border-radius: var(--radius-md);
  background: var(--surface-subtle); color: var(--ink-2); font-size: var(--fs-sm); cursor: text; }
.paste-target:focus-visible { outline: 2px solid var(--border-brand); outline-offset: 2px; }
.paste-target--disabled { opacity: .6; cursor: default; }
.image-previews { display: flex; gap: var(--sp-3); overflow-x: auto; margin-top: var(--sp-3); }
.image-preview { flex: 0 0 132px; margin: 0; }
.image-preview img { display: block; width: 132px; height: 88px; object-fit: contain;
  border: 1px solid var(--border-default); border-radius: var(--radius-sm); background: var(--surface-sunken); }
.image-preview figcaption { overflow: hidden; margin-top: var(--sp-1); color: var(--ink-4);
  font-size: var(--fs-2xs); text-overflow: ellipsis; white-space: nowrap; }
/* 依頼文は本文より一段小さく。会話の吹き出しと大きさを揃える。 */
.composer :deep(textarea) { font-size: var(--fs-sm); }
/* 添付・音声・モデル・依頼を1行に置く。モデルは中央、依頼は右端。 */
.input-row { display: grid; grid-template-columns: 1fr auto 1fr; gap: var(--sp-2);
  align-items: center; margin-top: var(--sp-3); }
.tools { display: flex; gap: var(--sp-2); align-items: center; }
.center { display: flex; gap: var(--sp-2); justify-content: center; }
.send { display: flex; justify-content: flex-end; }
.model { min-width: 190px; max-width: 260px; }
.effort { min-width: 110px; max-width: 150px; }
/* モデル名は操作の主役ではない。依頼文より小さくして、目立たせない。 */
.model :deep(.v-field__input), .effort :deep(.v-field__input),
.model :deep(.v-select__selection-text), .effort :deep(.v-select__selection-text) {
  font-size: var(--fs-2xs); }
.model :deep(.v-field__input), .effort :deep(.v-field__input) { min-height: var(--control-sm); }
@media (max-width: 720px) {
  .input-row { grid-template-columns: 1fr; }
  .center, .send { justify-content: flex-start; }
}
.attached { display: flex; gap: var(--sp-2); flex-wrap: wrap; margin-top: var(--sp-2); }
.hidden-input { display: none; }
.meta { color: var(--ink-4); font-size: var(--fs-xs); }
.input-help { margin-top: var(--sp-2); }
.revalidate { display: flex; align-items: center; gap: var(--sp-3); flex-wrap: wrap; margin-top: var(--sp-2); }
</style>
