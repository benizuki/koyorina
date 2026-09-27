/**
 * 本番のGCP基盤。開発（k3s）と同じKubernetesで動かす。
 *
 * 適用は承認後に行う。plan の内容を読み、費用と公開範囲を確かめてから apply する。
 * 状態ファイルはGCSに置く（backend.tf）。手元に残すと、他の人が触れない。
 */
terraform {
  required_version = ">= 1.6, < 2.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# 使うAPIだけ開ける。まとめて全部開けない。
resource "google_project_service" "required" {
  for_each = toset([
    "compute.googleapis.com",
    "container.googleapis.com",
    "artifactregistry.googleapis.com",
    # 取り込んだイメージの脆弱性検査。本番でArtifact Registryを使う理由がこれ。
    "containerscanning.googleapis.com",
    "secretmanager.googleapis.com",
    # ロードバランサの証明書を取り、更新も任せる。
    "certificatemanager.googleapis.com",
    # 保守用の接続はIAP経由にする。踏み台VMを作らないため。
    "iap.googleapis.com",
    "sqladmin.googleapis.com",
    # Cloud SQLへ私設IPで繋ぐための経路。
    "servicenetworking.googleapis.com",
    "aiplatform.googleapis.com",
    "iamcredentials.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

resource "google_compute_network" "forge" {
  name                    = "${var.name}-network"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.required]
}

resource "google_compute_subnetwork" "forge" {
  name          = "${var.name}-subnet"
  ip_cidr_range = var.subnet_cidr
  region        = var.region
  network       = google_compute_network.forge.id
  # Podとサービスの範囲は別に取る。後から広げられないため、最初に確保する。
  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = var.pod_cidr
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = var.service_cidr
  }
  private_ip_google_access = true
}

# 外向き通信だけ通す。ノードに外部IPを付けないため、NATが要る。
resource "google_compute_router" "forge" {
  name    = "${var.name}-router"
  region  = var.region
  network = google_compute_network.forge.id
}

resource "google_compute_router_nat" "forge" {
  name                               = "${var.name}-nat"
  router                             = google_compute_router.forge.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}
