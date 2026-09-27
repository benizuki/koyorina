import type { ColumnRole } from '@/types'

// 列の意味の選択肢。バックエンドの ColumnRole と COLUMN_ROLE_LABELS に合わせる。
export interface RoleOption { value: ColumnRole; title: string; icon: string }
export const roleGroups: { title: string; options: RoleOption[] }[] = [
  { title: '共通', options: [
    { value: 'none', title: '使用しない', icon: 'mdi-minus-circle-outline' },
    { value: 'timestamp', title: '日時・日付', icon: 'mdi-calendar-clock-outline' },
    { value: 'start_time', title: '開始日時', icon: 'mdi-clock-start' },
    { value: 'end_time', title: '終了日時', icon: 'mdi-clock-end' },
    { value: 'status', title: '状態', icon: 'mdi-list-status' },
    { value: 'category', title: '分類', icon: 'mdi-shape-outline' },
    { value: 'quantity', title: '数量', icon: 'mdi-counter' },
    { value: 'target', title: '目標値', icon: 'mdi-flag-checkered' },
    { value: 'value', title: '値', icon: 'mdi-numeric' },
    { value: 'unit', title: '単位', icon: 'mdi-ruler' },
  ] },
  { title: '製造現場', options: [
    { value: 'equipment', title: '設備・ライン', icon: 'mdi-robot-industrial-outline' },
    { value: 'product', title: '製品・品番', icon: 'mdi-package-variant-closed' },
    { value: 'lot', title: 'ロット', icon: 'mdi-tag-outline' },
    { value: 'result', title: '良否判定', icon: 'mdi-check-circle-outline' },
    { value: 'good_count', title: '良品数', icon: 'mdi-thumb-up-outline' },
    { value: 'defect_count', title: '不良数', icon: 'mdi-alert-circle-outline' },
    { value: 'defect_category', title: '不良の種類', icon: 'mdi-alert-box-outline' },
    { value: 'quality_item', title: '品質項目', icon: 'mdi-clipboard-list-outline' },
    { value: 'quality_value', title: '品質の測定値', icon: 'mdi-chart-bell-curve' },
    { value: 'specification_upper', title: '規格上限', icon: 'mdi-arrow-collapse-up' },
    { value: 'specification_lower', title: '規格下限', icon: 'mdi-arrow-collapse-down' },
  ] },
  { title: '事務・バックオフィス', options: [
    { value: 'location', title: '場所・拠点', icon: 'mdi-map-marker-outline' },
    { value: 'department', title: '部署', icon: 'mdi-account-group-outline' },
    { value: 'person', title: '担当者', icon: 'mdi-account-outline' },
    { value: 'partner', title: '取引先・顧客', icon: 'mdi-handshake-outline' },
    { value: 'document_number', title: '伝票番号', icon: 'mdi-file-document-outline' },
    { value: 'amount', title: '金額', icon: 'mdi-currency-jpy' },
    { value: 'unit_price', title: '単価', icon: 'mdi-tag-text-outline' },
    { value: 'due_date', title: '期日・納期', icon: 'mdi-calendar-alert-outline' },
    { value: 'note', title: '備考・メモ', icon: 'mdi-note-text-outline' },
  ] },
]
export const roleOptions: RoleOption[] = roleGroups.flatMap(group => group.options)
