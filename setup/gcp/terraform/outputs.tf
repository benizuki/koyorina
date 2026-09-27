output "entrance_ip" {
  value       = google_compute_address.entrance.address
  description = "ロードバランサのIP。DNSのAレコードをここへ向ける。"
}

output "dns_authorization_record" {
  value       = google_certificate_manager_dns_authorization.forge.dns_resource_record
  description = "証明書を取るために追加するCNAME。1度入れたら消さない（更新にも使う）。"
}

output "server_instance" {
  value = google_compute_instance.server.name
}

output "platform_registry" {
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.platform.repository_id}"
  description = "Koyorina本体のイメージを push する先。"
}

output "apps_registry" {
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.apps.repository_id}"
  description = "生成アプリのイメージ置き場。APP_REGISTRY_HOST にこの値を入れる。"
}

output "connect_command" {
  value       = "gcloud compute ssh ${google_compute_instance.server.name} --zone ${var.zone} --project ${var.project_id} -- sudo cat /etc/rancher/k3s/k3s.yaml"
  description = "kubeconfigの取り出し方。serverのアドレスは entrance_ip に書き換える。"
}

output "database_instance" {
  value       = google_sql_database_instance.forge.name
  description = "管理アプリのDB。公開IPは持たず、VPCの中からだけ届く。"
}

output "database_secret" {
  value       = google_secret_manager_secret.database_url.secret_id
  description = "接続URLの置き場。値はここでは出さない。配備時にSecret Managerから取り出す。"
}

# ── setup/ansible が読む値（terraform output -json で渡す） ──────────

output "server_internal_ip" {
  value       = google_compute_instance.server.network_interface[0].network_ip
  description = "k3sサーバの内部IP。エージェントがK3S_URLとして使う。"
}

output "server_name" {
  value = google_compute_instance.server.name
}

output "agent_internal_ips" {
  value       = [for agent in google_compute_instance.agent : agent.network_interface[0].network_ip]
  description = "生成・プレビュー用エージェントの内部IP一覧。"
}

output "agent_names" {
  value = [for agent in google_compute_instance.agent : agent.name]
}

output "vertex_service_account" {
  value       = google_service_account.gemini_workers.email
  description = "以前の token-file 方式の Vertex 認証で使っていた身元。WIF（システム設定）へ移行後は使わない。"
}

output "filestore_enabled" {
  value = var.enable_filestore
}

output "filestore_ip" {
  value       = var.enable_filestore ? google_filestore_instance.workspaces[0].networks[0].ip_addresses[0] : ""
  description = "FilestoreのIP。setup/ansibleのstorageロールがNFSサーバとして使う（enable_filestore時のみ）。"
}

output "filestore_share" {
  value       = var.enable_filestore ? google_filestore_instance.workspaces[0].file_shares[0].name : ""
  description = "Filestoreの共有名。"
}

output "workspace_disk" {
  value       = var.enable_filestore ? "" : google_compute_disk.workspaces[0].name
  description = "作業領域のディスク名（enable_filestore=falseのときのみ存在）。"
}

output "tenant_secret_key_secret" {
  value       = google_secret_manager_secret.tenant_secret_key.secret_id
  description = "APIキー暗号化鍵の置き場。値は配備時に deploy-app-gce.py が入れる。"
}
