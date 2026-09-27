/** イメージの置き場。
 *
 * - Artifact Registry(platform): Koyorina自身のイメージ
 * - Artifact Registry(apps): 生成アプリのイメージ。脆弱性検査が付き、
 *   結果をGCP側で追える
 *
 * クラスタ内のDocker Registry（`setup/manifest/registry.yaml`）はオンプレ・
 * GCPを使わない構成向けで、GCE構成では使わない（`setup/gcp/k8s/app-config.yaml`
 * が APP_REGISTRY_KIND=artifact にする）。使うとしても通常のPVC（StorageClass
 * 経由）で足りるため、専用のCompute Disk/サービスアカウントはここでは作らない。
 */
resource "google_artifact_registry_repository" "platform" {
  location      = var.region
  repository_id = var.name
  description   = "Koyorina本体のコンテナイメージ"
  format        = "DOCKER"
  depends_on    = [google_project_service.required]
}

resource "google_artifact_registry_repository" "apps" {
  location      = var.region
  repository_id = "${var.name}-apps"
  description   = "Koyorinaが生成したアプリのコンテナイメージ"
  format        = "DOCKER"
  # 脆弱性検査はプロジェクト単位で有効にする（containerscanning.googleapis.com）。
  depends_on = [google_project_service.required]
}

# 生成アプリのビルドPodが使う身元。Artifact Registryへ押し込む権限だけ与える。
resource "google_service_account" "builder" {
  account_id   = "${var.name}-builder"
  display_name = "Koyorina application image builder"
}

resource "google_artifact_registry_repository_iam_member" "builder" {
  location   = google_artifact_registry_repository.apps.location
  repository = google_artifact_registry_repository.apps.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.builder.email}"
}

# ノードは読むだけ。押し込む権限は持たせない。
# platform（koyorina/koyorina-agent/koyorina-preview-runtime。ノードが実際に
# pullするのはこちら）と apps（本番でAPP_REGISTRY_KIND=artifactにしたときの
# 生成アプリのイメージ置き場）の両方が要る。片方だけだと、もう片方のリポジトリの
# イメージがImagePullBackOffで止まる。
locals {
  node_service_accounts = toset([
    google_service_account.nodes.email,
    google_service_account.agent_nodes.email,
  ])
  node_registries = {
    platform = google_artifact_registry_repository.platform
    apps     = google_artifact_registry_repository.apps
  }
}

resource "google_artifact_registry_repository_iam_member" "nodes" {
  for_each = {
    for pair in setproduct(keys(local.node_registries), local.node_service_accounts) :
    "${pair[0]}-${pair[1]}" => {
      repository = local.node_registries[pair[0]]
      member     = pair[1]
    }
  }
  location   = each.value.repository.location
  repository = each.value.repository.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${each.value.member}"
}
