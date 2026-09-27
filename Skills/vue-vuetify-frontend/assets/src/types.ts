// バックエンドのレスポンス型はここに集約する。
// API を変えたらこのファイルを直す、という一箇所ルールにしておくと、
// 型エラーが「直すべき場所の一覧」として機能する。

// ── ユーザー・認証 ──────────────────────────────────
export interface User {
  email:   string
  name:    string
  picture: string
}

// 機能キーはアプリごとに決める。can_manage_users だけは共通で持たせておくと、
// ユーザー管理画面をそのまま流用できる。
export interface Permissions {
  can_view:         boolean
  can_edit:         boolean
  can_manage_users: boolean
}

export type Role = 'admin' | 'power' | 'general'

// "dev-bypass" は開発時に Google ログインを省いている状態。
// 画面は常時バナーを出して、気付かないまま使い続けるのを防ぐ。
export type AuthMode = 'google' | 'dev-bypass'

export interface MeResponse {
  user:        User
  role:        Role
  permissions: Permissions
  auth_mode?:  AuthMode
}

export interface UserRecord {
  email:       string
  emails:      string[]
  name:        string
  role:        Role
  permissions: Permissions
  enabled:     boolean
}

export type UserRecordInput = Omit<UserRecord, 'emails'> & { emails?: string[] }

// ── 共通のエラー形 ─────────────────────────────────
export interface ApiError {
  error:        string
  status_code?: number
}
