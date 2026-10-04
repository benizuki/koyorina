# 自己管理k3sのPD CSIだけが使う身元。初回クラスタ作成後、Kubernetesの
# OIDC issuer/JWKSを取り出して2回目のterraform applyで有効にする。
resource "google_service_account" "publication_pd_csi" {
  count        = var.publication_pd_csi_issuer == "" ? 0 : 1
  account_id   = "${var.name}-pd-csi"
  display_name = "Koyorina published app PD CSI controller"
  depends_on   = [google_project_service.required]
}

resource "google_project_iam_member" "publication_pd_csi_disks" {
  count   = var.publication_pd_csi_issuer == "" ? 0 : 1
  project = var.project_id
  role    = "roles/compute.storageAdmin"
  member  = "serviceAccount:${google_service_account.publication_pd_csi[0].email}"
}

resource "google_project_iam_custom_role" "publication_pd_csi_attach" {
  count       = var.publication_pd_csi_issuer == "" ? 0 : 1
  role_id     = "${replace(var.name, "-", "_")}_pd_csi_attach"
  title       = "Koyorina PD CSI attach"
  permissions = ["compute.instances.get", "compute.instances.attachDisk", "compute.instances.detachDisk"]
}

resource "google_project_iam_member" "publication_pd_csi_attach" {
  count   = var.publication_pd_csi_issuer == "" ? 0 : 1
  project = var.project_id
  role    = google_project_iam_custom_role.publication_pd_csi_attach[0].id
  member  = "serviceAccount:${google_service_account.publication_pd_csi[0].email}"
}

resource "google_service_account_iam_member" "publication_pd_csi_attach" {
  for_each = var.publication_pd_csi_issuer == "" ? {} : {
    server = google_service_account.nodes.name
    agent  = google_service_account.agent_nodes.name
  }
  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.publication_pd_csi[0].email}"
}

resource "google_iam_workload_identity_pool" "publication_pd_csi" {
  count                     = var.publication_pd_csi_issuer == "" ? 0 : 1
  workload_identity_pool_id = "${var.name}-pd-csi"
  display_name              = "Koyorina PD CSI"
  depends_on                = [google_project_service.required]
}

resource "google_iam_workload_identity_pool_provider" "publication_pd_csi" {
  count                              = var.publication_pd_csi_issuer == "" ? 0 : 1
  workload_identity_pool_id          = google_iam_workload_identity_pool.publication_pd_csi[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "k3s-csi"
  display_name                       = "Koyorina k3s PD CSI"
  attribute_mapping = {
    "google.subject" = "assertion.sub"
  }
  attribute_condition = "assertion.sub == 'system:serviceaccount:gce-pd-csi-driver:csi-gce-pd-controller-sa'"
  oidc {
    issuer_uri = var.publication_pd_csi_issuer
    jwks_json  = file(var.publication_pd_csi_jwks_file)
  }
}

resource "google_service_account_iam_member" "publication_pd_csi_wif" {
  count              = var.publication_pd_csi_issuer == "" ? 0 : 1
  service_account_id = google_service_account.publication_pd_csi[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/${google_iam_workload_identity_pool.publication_pd_csi[0].name}/subject/system:serviceaccount:gce-pd-csi-driver:csi-gce-pd-controller-sa"
}

output "publication_pd_csi_service_account" {
  value = var.publication_pd_csi_issuer == "" ? "" : google_service_account.publication_pd_csi[0].email
}

output "publication_pd_csi_wif_audience" {
  value = var.publication_pd_csi_issuer == "" ? "" : "//iam.googleapis.com/${google_iam_workload_identity_pool_provider.publication_pd_csi[0].name}"
}
