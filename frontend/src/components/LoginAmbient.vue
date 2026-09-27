<!-- ログイン画面の背景。ダークは星空、ライトは木漏れ日。
     テーマごとに「色」ではなく「描くもの」が変わるので、CSS の条件分岐ではなく
     どちらの層を描くかを Vue 側で選ぶ。こうしないと、同じ指定を
     data-theme と prefers-color-scheme の両方へ二重に書くことになり、
     片方だけ直して食い違う。

     位置は種を固定した擬似乱数で決める。毎回ばらつくと、テーマを切り替えた
     だけで星の配置が変わり、画面が作り直されたように見える。

     ここに出てくる px は「絵の寸法」で、UI の余白や部品の高さとは別物。
     星の直径やぼかしの半径をトークンの段（4px 刻み）に丸めると、
     星が点に見えなくなる。色はトークンから取る。 -->
<template>
  <div class="ambient" aria-hidden="true">
    <template v-if="effectiveTheme === 'dark'">
      <!-- 天の川のかわりの、ごく薄い明かり。星だけだと画面が平らに見える -->
      <div class="ambient__nebula ambient__nebula--high" />
      <div class="ambient__nebula ambient__nebula--low" />
      <span v-for="star in stars" :key="star.id" class="ambient__star"
        :class="{ 'ambient__star--flicker': star.flicker }" :style="star.style" />
    </template>
    <template v-else>
      <!-- 木の下の日陰。地が明るいままだと、光の粒を足しても「明るい点」にしか
           ならない。先に全体をわずかに沈めて、粒が抜けた穴として効くようにする -->
      <div class="ambient__canopy" />
      <!-- 差し込む方向。粒だけだと、光がどこから来たのか読めない -->
      <div class="ambient__sunwash" />
      <!-- 葉の重なり。光だけだと、何を透かしているのか分からない -->
      <div v-for="leaf in leaves" :key="leaf.id" class="ambient__leaf" :style="leaf.style" />
      <div v-for="ray in rays" :key="ray.id" class="ambient__ray" :style="ray.style" />
    </template>
  </div>
</template>

<script setup lang="ts">
import type { CSSProperties } from 'vue'
import { useThemeMode } from '@/composables/useThemeMode'

const { effectiveTheme } = useThemeMode()

/** 葉と光に共通の動き。
 *
 *  揺れ（位置）と明るさを**別々の周期**で持たせる。同じ周期で動かすと、
 *  全体が一斉に伸び縮みして、風ではなく画面全体の拡大縮小に見える。
 *  速さは「見れば動いていると分かる」ところまで上げる。12px を 20 秒で
 *  動かしていた版は毎秒 1.5px しか動かず、静止画と区別が付かなかった。 */
function motion(random: () => number, peak: number) {
  const swayFor = between(random, 5.5, 11)
  const breatheFor = between(random, 4, 9)
  return {
    '--patch-opacity': `${peak.toFixed(2)}`,
    '--sway-x': `${between(random, 16, 38).toFixed(1)}px`,
    '--sway-y': `${between(random, 8, 24).toFixed(1)}px`,
    animationDuration: `${swayFor.toFixed(2)}s, ${breatheFor.toFixed(2)}s`,
    animationDelay: `${(-random() * swayFor).toFixed(2)}s, ${(-random() * breatheFor).toFixed(2)}s`,
  }
}

/** 種から作る擬似乱数。同じ種なら毎回同じ並びになる。 */
function makeRandom(seed: number) {
  let state = seed
  return () => {
    state = (state + 0x6d2b79f5) | 0
    let t = Math.imul(state ^ (state >>> 15), 1 | state)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}
const pick = <T,>(random: () => number, values: T[]) => values[Math.floor(random() * values.length)]
const between = (random: () => number, min: number, max: number) => min + random() * (max - min)
/** 葉の隙間らしい、いびつな輪郭。整った楕円が並ぶと作り物に見える。 */
const BLOBS = [
  '58% 42% 63% 37% / 45% 55% 45% 55%',
  '46% 54% 38% 62% / 58% 42% 58% 42%',
  '63% 37% 52% 48% / 38% 62% 38% 62%',
  '40% 60% 55% 45% / 52% 44% 56% 48%',
]

/** 星。上ほど密に、下ほど疎に置く。空は上が深く、下は地明かりで見えにくい。 */
const stars = (() => {
  const random = makeRandom(20260922)
  return Array.from({ length: 170 }, (_, id) => {
    // ゆるく上へ寄せる。2 つの乱数の小さいほうを採ると寄りすぎて、
    // 画面の下半分がほとんど空になる（実機で下に星が無いと言われた）。
    const top = random() ** 1.25 * 100
    // 3 割ほどは短く強く光らせる。全部をゆっくり明滅させると、
    // 変化が遅すぎて「瞬いている」と受け取れない（実測で 0.5 秒あたり 0.05 程度）。
    // 瞬く星は少し大きくする。1px の点が一瞬光っても目に留まらない。
    const flicker = random() < .3
    const size = flicker ? between(random, 1.5, 2.8)
      : random() < .78 ? between(random, .9, 1.8) : between(random, 2, 3.2)
    const color = pick(random, ['var(--ambient-star)', 'var(--ambient-star)',
      'var(--ambient-star-cool)', 'var(--ambient-star-warm)'])
    const brightness = flicker ? between(random, .85, 1)
      : size > 2 ? between(random, .75, 1) : between(random, .3, .7)
    return {
      id,
      flicker,
      style: {
        top: `${top}%`,
        left: `${random() * 100}%`,
        width: `${size}px`,
        height: `${size}px`,
        background: color,
        // 大きい星だけ、にじみを持たせる。全部に付けるとぼやけた点の集まりになる。
        boxShadow: size > 2 ? `0 0 ${size * 2.5}px ${color}` : 'none',
        '--star-peak': `${brightness}`,
        '--star-dip': `${brightness * (flicker ? .16 : .3)}`,
        animationDuration: `${(flicker ? between(random, 2.6, 6) : between(random, 3.4, 7.8)).toFixed(2)}s`,
        animationDelay: `${(-random() * 8).toFixed(2)}s`,
      } as CSSProperties,
    }
  })
})()

/** 木漏れ日。
 *
 *  葉の隙間を通った光は、画面へ均等には散らない。枝ぶりに沿って**かたまり**で
 *  落ち、その中に大小の粒が重なる。等間隔に置くと、木漏れ日ではなく
 *  「明るい点が並んだ壁紙」になる。
 *  そこで、いくつかの中心の周りへ粒を寄せる。中心はカードを避けた位置に置く。 */
const CLUSTERS = [
  { x: 6, y: 12 }, { x: 90, y: 20 }, { x: 12, y: 54 }, { x: 88, y: 58 },
  { x: 22, y: 88 }, { x: 74, y: 92 }, { x: 48, y: 3 },
]
/** 中心からの散らばり。2 つの乱数の差を使うと、中心に寄った分布になる。 */
const scatter = (random: () => number, spread: number) => (random() - random()) * spread

const rays = (() => {
  const random = makeRandom(915)
  return CLUSTERS.flatMap((center, group) =>
    Array.from({ length: 4 }, (_, index) => {
      // 葉の重なりが薄い場所に当たる、日なた側の光。輪郭を残す。
      const size = between(random, 40, 130)
      return {
        id: `${group}-${index}`,
        style: {
          top: `${center.y + scatter(random, 22)}%`,
          left: `${center.x + scatter(random, 18)}%`,
          width: `${size}px`,
          height: `${size * between(random, .55, .9)}px`,
          borderRadius: pick(random, BLOBS),
          /* 右側はログインカードの余白。左の木漏れ日より一段落として、
             光が先に目へ入らないようにする。 */
          opacity: `${between(random, center.x >= 70 ? .12 : .5, center.x >= 70 ? .28 : .9).toFixed(2)}`,
          filter: `blur(${between(random, 4, 12).toFixed(1)}px)`,
          '--sway-tilt': `${between(random, -28, -4).toFixed(1)}deg`,
          animationDuration: `${between(random, 11, 21).toFixed(2)}s`,
          animationDelay: `${(-random() * 12).toFixed(2)}s`,
        } as CSSProperties,
      }
    }))
})()

/** 葉。
 *
 *  木漏れ日は「明るい点を足したもの」ではなく、**葉が光を遮った残り**なので、
 *  光の粒を並べても、ただの丸い光が浮いているようにしか見えない。
 *  そこで葉を重ねて敷き、その隙間が明るく残るようにする。
 *  1 枚 1 枚は薄く、重なったところだけ濃くなる。 */
const leaves = (() => {
  const random = makeRandom(4471)
  return CLUSTERS.flatMap((center, group) =>
    Array.from({ length: 9 }, (_, index) => {
      // 3 枚に 1 枚は小さく、輪郭も残す。細かい葉が無いと、
      // 大きなぼかしだけが残って「緑の靄」になる。
      const small = index % 3 === 0
      const size = small ? between(random, 26, 80) : between(random, 80, 240)
      const rightSide = center.x >= 70
      return {
        id: `${group}-${index}`,
        style: {
          top: `${center.y + scatter(random, 30)}%`,
          left: `${center.x + scatter(random, 26)}%`,
          width: `${size}px`,
          height: `${size * between(random, .5, .82)}px`,
          borderRadius: pick(random, BLOBS),
          filter: `blur(${(small ? between(random, 2, 6) : between(random, 8, 22)).toFixed(1)}px)`,
          '--sway-tilt': `${between(random, -34, -2).toFixed(1)}deg`,
          ...motion(random, rightSide
            ? (small ? between(random, .07, .16) : between(random, .05, .12))
            : (small ? between(random, .2, .46) : between(random, .16, .36))),
        } as CSSProperties,
      }
    }))
})()
</script>

<style scoped>
.ambient {
  position: absolute;
  inset: 0;
  z-index: 0;
  overflow: hidden;
  pointer-events: none;
  /* 上下の縁で消す。切らないと、背景が四角い板として見えてしまう。 */
  mask-image: linear-gradient(to bottom, transparent, #000 8%, #000 94%, transparent);
}

/* ── 星空 ───────────────────────────────────────────────── */
.ambient__star {
  position: absolute;
  border-radius: var(--radius-pill);
  opacity: var(--star-dip);
  /* またたきは明るさだけ。位置を動かすと、空ごと流れて見える。 */
  animation: ambient-twinkle ease-in-out infinite alternate;
}
/* ひと呼吸で強く光り、あとは沈んでいる星。周期の大半を暗いまま過ごし、
   一瞬だけ持ち上げる。均等に明滅させると「ゆっくり呼吸する点」にしか見えない。 */
.ambient__star--flicker {
  animation-name: ambient-flicker;
  animation-direction: normal;
  animation-timing-function: ease-out;
}
.ambient__nebula {
  position: absolute;
  border-radius: var(--radius-pill);
  background: radial-gradient(closest-side, var(--ambient-night-glow), transparent);
  /* 形が読めるほど濃いと雲に見える。気配だけ残す。 */
  opacity: .34;
}
.ambient__nebula--high {
  top: -22%; left: 44%;
  width: 62%; height: 70%;
  filter: blur(60px);
  animation: ambient-drift 42s ease-in-out infinite alternate;
}
.ambient__nebula--low {
  bottom: -30%; left: -14%;
  width: 78%; height: 62%;
  filter: blur(70px);
  opacity: .42;
  animation: ambient-drift 54s ease-in-out infinite alternate-reverse;
}

/* ── 木漏れ日 ───────────────────────────────────────────── */
.ambient__canopy {
  position: absolute;
  inset: 0;
  background: var(--ambient-leaf);
  opacity: .12;
}
.ambient__sunwash {
  position: absolute;
  top: -30%; left: -18%;
  width: 86%; height: 96%;
  border-radius: var(--radius-pill);
  background: radial-gradient(closest-side, var(--ambient-sun), transparent);
  opacity: .3;
  filter: blur(60px);
  animation: ambient-drift 38s ease-in-out infinite alternate;
}
.ambient__ray {
  position: absolute;
  background: radial-gradient(closest-side, var(--ambient-sun-core), var(--ambient-sun) 55%, transparent);
  animation-name: ambient-sway, ambient-breathe;
}
.ambient__leaf {
  position: absolute;
  background: radial-gradient(closest-side, var(--ambient-leaf), transparent);
  animation-name: ambient-sway, ambient-breathe;
}
/* 揺れも明滅も、周期と遅れは要素ごとに inline で入れる。
   ここで決めるのは「どう動くか」だけにする。 */
.ambient__ray, .ambient__leaf {
  opacity: var(--patch-opacity);
  animation-timing-function: ease-in-out;
  animation-iteration-count: infinite;
  animation-direction: alternate;
  animation-fill-mode: both;
}

@keyframes ambient-flicker {
  0%, 58% { opacity: var(--star-dip); }
  66% { opacity: var(--star-peak); }
  70% { opacity: calc(var(--star-peak) * .4); }
  75% { opacity: var(--star-peak); }
  88%, 100% { opacity: var(--star-dip); }
}
@keyframes ambient-twinkle {
  from { opacity: var(--star-dip); }
  to { opacity: var(--star-peak); }
}
/* 揺れ幅と速さは要素ごとに変える（--sway-x / --sway-y と inline の周期）。
   全部を同じ幅で動かすと、葉むらではなく一枚の板がずれて見える。
   拡大縮小は入れない。ぼかした面を拡大すると毎フレーム描き直しになるが、
   平行移動なら一度描いた面をずらすだけで済む。 */
@keyframes ambient-sway {
  from { transform: translate3d(0, 0, 0) rotate(var(--sway-tilt, 0deg)); }
  to {
    transform:
      translate3d(var(--sway-x, 16px), var(--sway-y, 10px), 0)
      rotate(var(--sway-tilt, 0deg));
  }
}
/* 葉が揺れると隙間が開いたり閉じたりする。その明暗を、揺れとは別の周期で。 */
@keyframes ambient-breathe {
  from { opacity: calc(var(--patch-opacity) * .45); }
  to { opacity: var(--patch-opacity); }
}
@keyframes ambient-drift {
  from { transform: translate3d(0, 0, 0); }
  to { transform: translate3d(-4%, 3%, 0); }
}

/* 動きを減らす設定のときは、配置はそのままに止める。 */
@media (prefers-reduced-motion: reduce) {
  .ambient__star, .ambient__ray, .ambient__leaf,
  .ambient__nebula, .ambient__sunwash { animation: none; }
  .ambient__star { opacity: var(--star-peak); }
}
</style>
