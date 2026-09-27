export interface User { id: string; email: string; display_name: string; role: string
  can_manage_users: boolean; can_develop: boolean; can_use_codex: boolean; department_id: string | null
  // 所属しているテナント。
  tenant_ids?: string[]
  // アプリを作れるテナント（そこでのロールが admin / developer）。画面は選べるものだけを出す。
  develop_tenant_ids?: string[]
  // テナントごとのロール（システム管理者は全テナントで admin）。
  tenant_roles?: Record<string, string>
  // 生成AIの設定と利用状況を扱えるテナント（システム管理者は全テナント、テナント管理者は自分の分）。
  admin_tenant_ids?: string[]
  auth_mode: 'google' | 'dev-bypass' }
export interface Role { id: string; label: string }
// システムロール（admin / member）と、テナントごとのロール（admin / developer / user）。
export interface RoleCatalog { system: Role[]; tenant: Role[] }
// 所属テナントと、そこでのロール。
export interface TenantMembership { tenant_id: string; role: string }
export interface Department { id: string; name: string; note: string; enabled: boolean }
export interface ManagedUser {
  id: string; email: string; display_name: string; google_subject: string | null
  role: string; enabled: boolean; codex_enabled: boolean; department_id: string | null; department_name: string | null
  // 所属テナントと、そこでのロール。複数可。身元はグループで1つ、ロールはテナントごと。
  tenants?: TenantMembership[]
  tenant_ids?: string[] }
export interface AiUsageTenantRow {
  tenant_id: string | null; tenant_name: string; projects: number; requests: number; failed: number
  input_tokens: number; output_tokens: number; total_tokens: number
  tokenized_requests: number; duration_seconds: number }
export interface AiUsageRow {
  user_id: string; email: string; display_name: string; codex_enabled: boolean
  requests: number; succeeded: number; failed: number; codex_requests: number; gemini_requests: number
  input_tokens: number; output_tokens: number; cached_tokens: number; total_tokens: number; tokenized_requests: number
  duration_seconds: number
}
export interface AiUsageProject {
  project_id: string; project_name: string; requests: number; failed: number
  input_tokens: number; output_tokens: number; total_tokens: number; tokenized_requests: number; duration_seconds: number
}
export interface AiUsageProviderRow {
  provider: 'codex' | 'gemini' | 'antigravity' | 'openai_compatible' | 'claude'; requests: number; succeeded: number; failed: number
  input_tokens: number; output_tokens: number; cached_tokens: number; total_tokens: number
  tokenized_requests: number; duration_seconds: number
}
export interface AiUsageSummary {
  days: number; since: string; start: string; end: string; today_requests: number
  totals: Omit<AiUsageRow, 'user_id' | 'email' | 'display_name' | 'codex_enabled'>
  daily: { date: string; requests: number }[]; providers: AiUsageProviderRow[]
  projects: AiUsageProject[]; users: AiUsageRow[]
  tenants: AiUsageTenantRow[]
}
export interface StorageClaimUsage {
  status: 'ok' | 'missing' | 'error' | 'unavailable' | 'not_measured'; claim_name: string
  requested_bytes: number; used_bytes: number; capacity_bytes: number; available_bytes: number
  usage_percent: number; level: 'ok' | 'warning' | 'danger' | 'unknown'
  measured_at: string | null; error: string | null
}
export interface StorageUsageSummary {
  measured_at: string | null
  node: { capacity_bytes: number; available_bytes: number; used_bytes: number
          usage_percent: number; level: 'ok' | 'warning' | 'danger' | 'unknown' }
  totals: { requested_bytes: number; used_bytes: number }
  tenants: { tenant_id: string; tenant_name: string; generation: StorageClaimUsage
             preview: StorageClaimUsage; used_bytes: number; requested_bytes: number
             level: 'ok' | 'warning' | 'danger' | 'unknown' }[]
}
export interface FieldSpec { name: string; kind: 'text' | 'longtext' | 'number' | 'date' | 'bool'; required: boolean }
export interface TableSpec { name: string; kind: 'record' | 'master'; fields: FieldSpec[] }
export type ColumnRole = 'none' | 'timestamp' | 'equipment' | 'product' | 'lot' | 'quantity' | 'result'
  | 'good_count' | 'defect_count' | 'defect_category' | 'status' | 'start_time' | 'end_time'
  | 'target' | 'quality_value' | 'quality_item' | 'unit' | 'specification_upper'
  | 'specification_lower' | 'planned_start' | 'planned_end' | 'actual_start' | 'actual_end'
  | 'category' | 'value'
  | 'location' | 'department' | 'person' | 'partner' | 'document_number' | 'amount' | 'unit_price'
  | 'due_date' | 'note'
export interface SampleColumn { name: string; kind: 'text' | 'number' | 'date' | 'datetime' | 'bool'; samples: string[]; suggested_role: ColumnRole }
export interface SampleDataProfile { filename: string; sheet: string | null; row_count: number; columns: SampleColumn[]; warnings: string[] }
export interface ColumnMapping { column: string; role: ColumnRole }
export interface CreationProfile {
  app_pattern: 'local_file_visualization' | 'data_management' | 'file_import'
  // prompt: 依頼文だけで作ったアプリ。省略時は従来の手順（guided）。
  mode?: 'guided' | 'prompt'
  work_category: 'manufacturing' | 'indirect' | 'other'
  app_type: 'records' | 'visualization' | 'both'
  goals: string[]; other_goal: string; production_day_start: string | null
  // 1日の稼働時間（24は1日通し）と、開いたときの期間（データの最新の日から数える）。
  production_day_hours?: 8 | 12 | 16 | 24 | null
  default_period?: 'latest_day' | 'last_3_days' | 'last_7_days' | 'last_30_days' | 'all' | null
  sample_data: SampleDataProfile | null; column_mappings: ColumnMapping[]
}
export interface ProjectInput { name: string; purpose: string; audience?: 'self' | 'team'; fields: FieldSpec[]; tables: TableSpec[]; requirements?: string[]; generation_prompt?: string; creation_profile?: CreationProfile | null }
export interface PurposeDraft { purpose: string }
export interface DevelopmentSession {
  holder_id: string | null; holder_name: string | null
  mine: boolean; editable: boolean; can_take_over: boolean; started_at: string | null
}
export interface CodeHistoryEntry { commit: string; at: string; author: string; summary: string }
export interface CodeHistoryDiff {
  commit: string; text: string; truncated: boolean
  files: { change: string; path: string }[] }
export interface Tenant { id: string; name: string; note: string; enabled: boolean }
/** テナントの生成アプリが使うGemini。APIキーは値を返さず、設定済みかだけ分かる。 */
export interface TenantAiSettings {
  backend: 'none' | 'gemini_api' | 'vertex'
  api_key_configured: boolean; secrets_available: boolean
  gcp_project: string; location: string; model: string
  thinking_level: '' | 'MINIMAL' | 'LOW' | 'MEDIUM' | 'HIGH'
  wif_project_number: string; wif_pool_id: string; wif_provider_id: string; wif_service_account: string
}
/** 接続テストの結果。失敗したときは止まった段階（step）とGoogleが返した理由が入る。 */
export interface GeminiProbeResult {
  ok: boolean; step: string; status?: number | null; message?: string | null
  model?: string | null; text?: string | null; elapsed_ms?: number | null
}
/** Koyorina 自身が使う Gemini（システム設定）。Gemini API か Vertex AI（WIF）の2択。
 *  backend=env はまだ保存していない状態で、環境の設定を使っている。 */
export interface SystemGemini {
  backend: 'env' | 'gemini_api' | 'vertex'
  api_key_configured: boolean; secrets_available: boolean
  gcp_project: string; location: string; model: string
  thinking_level: '' | 'MINIMAL' | 'LOW' | 'MEDIUM' | 'HIGH'
  wif_project_number: string; wif_pool_id: string; wif_provider_id: string
  wif_service_account: string
  // 環境の設定（backend=env のときに使われる値）。画面では変えない。
  environment: { backend: 'gemini_api' | 'vertex'; gcp_project: string; location: string; model: string }
  agents_synced?: boolean | null
}
/** システム設定の Antigravity／OpenAI互換API。saved=false はまだ保存しておらず、環境の設定を使っている。 */
export interface SystemLlm {
  enabled: boolean; saved: boolean; api_key_configured: boolean; secrets_available: boolean
  model?: string; agent?: string; max_total_tokens?: number; label?: string; base_url?: string
  // Claude だけ。api_key=Anthropic の APIキー / vertex=Claude on Vertex AI。
  backend?: 'api_key' | 'vertex'; gcp_project?: string; location?: string
  environment: { enabled: boolean; model: string; label?: string; base_url?: string; backend?: string }
  agents_synced?: boolean | null
}
/** テナントの生成AI。mode は system=システムの既定を使う／tenant=このテナントの設定／disabled=使わせない。 */
export type TenantLlmMode = 'system' | 'tenant' | 'disabled'
export interface TenantLlmState<T> { kind: string; mode: TenantLlmMode; settings: T | null; system: T
  agents_synced?: boolean | null }
export interface TenantLlmSettings { gemini: TenantLlmState<SystemGemini>
  antigravity: TenantLlmState<SystemLlm>; openai_compatible: TenantLlmState<SystemLlm>
  claude: TenantLlmState<SystemLlm> }
export interface SystemWorkloadIdentity { app_subject: string; agent_subject: string; issuer: string; jwks: unknown }
/** GCP側で信頼を登録するための値。秘密ではない。 */
export interface WorkloadIdentityInfo {
  namespace: string; service_account: string; subject: string; issuer: string; jwks: unknown
}
export interface Collaborator { user_id: string; name: string; email: string; role?: string; added_at?: string }
export interface CollaboratorView { owner_id: string; can_administer: boolean; collaborators: Collaborator[] }
export interface Project extends ProjectInput {
  owner_id: string; owner_name?: string; owner_email?: string
  // 画面が出し分けるための可否。押せるのにエラーになる操作を見せない。
  is_owner?: boolean; can_edit?: boolean; can_administer?: boolean; collaborator_count?: number
  // 生成・プレビューの保存領域と利用権限を分離する事業単位。
  tenant_id?: string | null
  audience: 'self' | 'team'
  id: string; status: 'draft' | 'approved'; revision: number; approved_revision: number | null; updated_at: string
  generation_prompt: string
  specification: { screens: string[]; stack: string; preview: string; production: string; storage: string; creation_profile?: CreationProfile | null }
}
export interface Config { auth_mode: 'google' | 'dev-bypass'; google_client_id: string; generation_ready: boolean; codex_available: boolean; gemini_available: boolean; antigravity_available: boolean; openai_compatible_available: boolean; claude_available?: boolean; openai_compatible_label: string; pdf_extraction_enabled: boolean; preview_enabled: boolean
  // 公開サービスでは閉じておく口。サーバーの設定で有効なときだけ画面に出す。
  preview_shell_enabled: boolean; local_codex_enabled: boolean }
export interface ExtractedFields { fields: FieldSpec[]; warnings: string[] }
export interface DeviceLogin { verification_url: string; user_code: string; expires_at: string }
export interface CodexStatus {
  status: 'unavailable' | 'preparing' | 'disconnected' | 'pending' | 'connected'
  // 運用側の既定の生成元。Geminiが既定なら、ChatGPTの接続は要らない。
  generator?: 'codex' | 'gemini'
  email: string | null; plan: string | null; login: DeviceLogin | null; error: string | null; busy: boolean
}
export type GeneratorRuntimeState = 'running' | 'starting' | 'stopped' | 'error' | 'unavailable'
export interface GeneratorRuntime {
  state: GeneratorRuntimeState; pods: number; running: number; starting: number; errors: number
}
export interface GeneratorRuntimes { codex: GeneratorRuntime; gemini: GeneratorRuntime; antigravity: GeneratorRuntime; openai_compatible: GeneratorRuntime
  claude: GeneratorRuntime }
export interface InterviewQuestionOption { label: string; description: string }
export interface InterviewQuestion { id: string; header: string; question: string; options: InterviewQuestionOption[]; is_other?: boolean }
export interface RequirementsInterview {
  status: 'idle' | 'starting' | 'thinking' | 'waiting' | 'completed' | 'failed' | 'cancelled'
  provider?: 'codex' | 'gemini' | 'antigravity'; question?: InterviewQuestion | null; questions?: InterviewQuestion[]; result?: ProjectInput | null; error?: string | null
  // 何回目のやり取りか。何問で終わるか分からないと、答える側が見通せない。
  round?: number; rounds?: number
}
export interface GenerationJob {
  id: string; project_id: string; chat_id: string; revision: number; status: 'starting' | 'generating' | 'generated' | 'failed'
  instruction: string | null; attachments: string[]
  error: string | null; created_at: string; updated_at: string; source_type: 'managed_codex' | 'local_codex'
  // 生成AIの最後の報告（Markdown、伏せ字済み）と、今回見送った機能。
  summary?: string | null; next_steps?: string[]
  provider: 'codex' | 'gemini' | 'antigravity' | 'openai_compatible' | 'claude' | null; model: string | null; effort: string | null
  usage: { input_tokens: number | null; output_tokens: number | null; cached_tokens: number | null; total_tokens: number | null }
  // 実行環境へ届かず状態を確かめられなかったときだけ真。失敗ではない。
  unconfirmed?: boolean; unconfirmed_reason?: string
}
export interface PreviewStatus {
  enabled: boolean; state: 'stopped' | 'starting' | 'running' | 'failed'; url: string | null
  port: number | null; job_id: string | null; updated_at: string | null; message: string | null
  // 失敗したときだけ入る。hint は次にやること、evidence は根拠にしたログの1行。
  hint: string | null; evidence: string | null
}
export interface ClusterPod {
  name: string; phase: string; ready: number; containers: number; restarts: number
  node: string; age: string; images: string[] }
export interface ClusterService { name: string; type: string; cluster_ip: string; ports: string[] }
export interface ClusterDeployment { name: string; ready: number; desired: number; age: string }
export interface ClusterState {
  available: boolean
  namespaces: { namespace: string; pods: ClusterPod[]; services: ClusterService[]
                deployments: ClusterDeployment[] }[] }
export interface PodLogs { namespace: string; pod: string; lines: string[] }
export interface NetworkAuditRow {
  source: string; destination: string; destination_ip: string | null
  protocol: string; port: number | null; result: 'allowed' | 'blocked' | 'reset' | 'unknown'
  reason: string; count: number; last_seen: string
}
export interface NetworkAuditSummary {
  available: boolean; window_minutes: number; observed: number; truncated: boolean
  totals: { allowed: number; blocked: number; reset: number; unknown: number }
  readers?: { available: number; total: number }
  rows: NetworkAuditRow[]
}
