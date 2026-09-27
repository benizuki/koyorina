/** 管理アプリのDB。本番はCloud SQL（PostgreSQL）を使う。
 *
 * 手元の開発環境はクラスタ上のPostgreSQLのまま。本番でCloud SQLにするのは、
 * バックアップ・復旧・パッチ当てを自分たちで持たずに済むため。
 * 外向きのIPは持たせない。VPCの中のノードから私設IPで届く。
 */

# Cloud SQLへ私設IPで繋ぐための経路。先に範囲を確保してから接続を張る。
resource "google_compute_global_address" "database" {
  name          = "${var.name}-db-range"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.forge.id
}

resource "google_service_networking_connection" "database" {
  network                 = google_compute_network.forge.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.database.name]
  depends_on              = [google_project_service.required]
}

resource "google_sql_database_instance" "forge" {
  name             = "${var.name}-db"
  database_version = "POSTGRES_17"
  region           = var.region
  # 消す操作は明示的に外してから行う。取り違えると、業務データがそのまま消える。
  deletion_protection = true

  settings {
    edition           = "ENTERPRISE"
    tier              = var.database_tier
    availability_type = var.database_high_availability ? "REGIONAL" : "ZONAL"
    disk_type         = "PD_SSD"
    disk_size         = var.database_disk_gb
    disk_autoresize   = true

    ip_configuration {
      # 公開IPは付けない。到達できるのはVPCの中だけにする。
      ipv4_enabled                                  = false
      private_network                               = google_compute_network.forge.id
      enable_private_path_for_google_cloud_services = true
      ssl_mode                                      = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled                        = true
      start_time                     = "18:00" # UTC。日本時間の午前3時
      point_in_time_recovery_enabled = true
      transaction_log_retention_days = 7
      backup_retention_settings {
        retained_backups = 30
        retention_unit   = "COUNT"
      }
    }

    maintenance_window {
      day          = 7 # 日曜
      hour         = 19
      update_track = "stable"
    }

    database_flags {
      # 接続元を追えるようにする。障害時に、どのPodが握っていたかを見る。
      name  = "log_connections"
      value = "on"
    }

    insights_config {
      query_insights_enabled = true
    }
  }

  depends_on = [google_service_networking_connection.database]
}

resource "google_sql_database" "forge" {
  name     = "forge"
  instance = google_sql_database_instance.forge.name
}

# 鍵は手で作らず、ここで作ってSecret Managerへ入れる。値は出力しない。
resource "random_password" "database" {
  length  = 32
  special = false
}

resource "google_sql_user" "forge" {
  name     = "forge"
  instance = google_sql_database_instance.forge.name
  password = random_password.database.result
}

resource "google_secret_manager_secret" "database_url" {
  secret_id = "${var.name}-database-url"
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "database_url" {
  secret = google_secret_manager_secret.database_url.id
  secret_data = format(
    "postgresql+psycopg://%s:%s@%s:5432/%s",
    google_sql_user.forge.name,
    random_password.database.result,
    google_sql_database_instance.forge.private_ip_address,
    google_sql_database.forge.name,
  )
}

# 接続URLはノードのサービスアカウントで読み出し、配備時にk8sのSecretへ入れる。
# Podへ直接GCPの身元は渡さない。渡すと、生成アプリ側から触れる面が増える。
resource "google_secret_manager_secret_iam_member" "nodes_database_url" {
  secret_id = google_secret_manager_secret.database_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.nodes.email}"
}

# DBに暗号化して保存するAPIキーの鍵(TENANT_SECRET_KEY)。DBと同じ寿命で持つ。
# クラスタの中にしか無いと、クラスタを作り直した時点で鍵が変わり、Cloud SQLに
# 残っている暗号化済みのAPIキーが開けなくなる。
# 置き場だけをここで作り、値は deploy-app-gce.py が生成して入れる
# （既存クラスタの鍵があればそれを上げる）。tfstateへ鍵を載せないため。
resource "google_secret_manager_secret" "tenant_secret_key" {
  secret_id = "${var.name}-tenant-secret-key"
  replication {
    auto {}
  }
  # 消すと保存済みのAPIキーを二度と開けない。Cloud SQLと同じく保護する。
  deletion_protection = true
  depends_on          = [google_project_service.required]
}

# サーバVMは読むのと、最初の1版を足すことだけができる（版の無効化・削除はできない）。
resource "google_secret_manager_secret_iam_member" "nodes_tenant_secret_key_read" {
  secret_id = google_secret_manager_secret.tenant_secret_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.nodes.email}"
}

resource "google_secret_manager_secret_iam_member" "nodes_tenant_secret_key_add" {
  secret_id = google_secret_manager_secret.tenant_secret_key.id
  role      = "roles/secretmanager.secretVersionAdder"
  member    = "serviceAccount:${google_service_account.nodes.email}"
}
