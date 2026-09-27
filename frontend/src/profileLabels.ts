import type { CreationProfile } from '@/types'

// 可視化アプリの「開いたときの期間」。直近はデータの最新の日から数える（backend の PERIOD_LABELS と揃える）。
export const periodLabels: Record<NonNullable<CreationProfile['default_period']>, string> = {
  latest_day: '直近1日', last_3_days: '直近3日間', last_7_days: '直近7日間',
  last_30_days: '直近30日', all: '全期間',
}

export const dayHoursLabel = (hours: number) => hours === 24 ? '24時間（1日通し）' : `${hours}時間`
