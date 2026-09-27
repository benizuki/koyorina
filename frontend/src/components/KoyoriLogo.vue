<!-- こよりの三本の糸をより合わせた銘。currentColorで明暗テーマに追従する。 -->
<template>
  <svg class="koyori-logo" :width="size" :height="size" viewBox="0 0 32 32"
    role="img" :aria-label="APP_NAME" focusable="false">
    <path class="koyori-logo__strand koyori-logo__strand--a" d="M7 4c8 4 10 8 8 12S11 24 5 28" />
    <path class="koyori-logo__strand koyori-logo__strand--b" d="M14 3c7 5 8 9 5 13s-4 8 2 13" />
    <path class="koyori-logo__strand koyori-logo__strand--c" d="M21 4c-5 5-5 9-1 13s5 7 7 11" />
    <circle class="koyori-logo__dot" cx="25" cy="5" r="1.8" />
  </svg>
</template>

<script setup lang="ts">
import { APP_NAME } from '@/branding'
withDefaults(defineProps<{ size?: number }>(), { size: 28 })
</script>

<style scoped>
.koyori-logo { display: block; flex: none; overflow: visible; }
.koyori-logo__strand { fill: none; stroke: currentColor; stroke-linecap: round; stroke-width: 2.4; }
.koyori-logo__strand--a { opacity: .65; }
.koyori-logo__strand--b { stroke-width: 2.8; }
.koyori-logo__strand--c { opacity: .82; }
.koyori-logo__dot { fill: var(--text-brand); }
</style>
