/** k3sをGCEの上に置く。
 *
 * GKEではなくこちらにしたのは、費用と、いまの作りとの相性のため。
 * Codexのサンドボックス用seccompプロファイルはノードのファイルとして要る。
 * 自分のVMなら起動スクリプトで置くだけで済む（GKEでは配る仕掛けが要った）。
 *
 * 構成: サーバ1台（制御と管理アプリ）＋ エージェント（生成とプレビュー）。
 * 1台構成にもできる。その場合 agent_count = 0。
 */

/* ノードの身元。
 *
 * GCEではメタデータサーバからADCが取れるので、SA鍵もWIFも要らない。
 * ただしノード上のPodは全部この身元を使えてしまうため、VMの役割ごとに分ける。
 * 生成アプリと生成の実行環境からは、メタデータサーバへの通信をNetworkPolicyで塞ぐ。
 */
resource "google_service_account" "nodes" {
  account_id   = "${var.name}-nodes"
  display_name = "Koyorina k3s server"
}

resource "google_service_account" "agent_nodes" {
  account_id   = "${var.name}-agent-nodes"
  display_name = "Koyorina k3s agent"
}

resource "google_project_iam_member" "nodes" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/artifactregistry.reader",
    # PDFの項目読み取りと、Geminiでの生成。鍵を置かずにこれで足りる。
    "roles/aiplatform.user",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.nodes.email}"
}

resource "google_project_iam_member" "agent_nodes" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/artifactregistry.reader",
    "roles/aiplatform.user",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.agent_nodes.email}"
}

# エージェントがサーバへ参加するための合言葉。setup/ansibleは既定でSSH越しに
# サーバの /var/lib/rancher/k3s/server/node-token を直接読むので、これは
# 通常不要（publish_join_token=falseのまま）。VMからGCP外へトークンを一切
# 出したくない、あるいはAnsibleの実行元からサーバへSSHできない構成のときだけ
# publish_join_token=true にする。
resource "google_secret_manager_secret" "join_token" {
  count     = var.publish_join_token ? 1 : 0
  secret_id = "${var.name}-k3s-join-token"
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_iam_member" "join_token_write" {
  count     = var.publish_join_token ? 1 : 0
  secret_id = google_secret_manager_secret.join_token[0].id
  role      = "roles/secretmanager.secretVersionManager"
  member    = "serviceAccount:${google_service_account.nodes.email}"
}

resource "google_secret_manager_secret_iam_member" "join_token_read" {
  for_each = var.publish_join_token ? toset([
    google_service_account.nodes.email,
    google_service_account.agent_nodes.email,
  ]) : []
  secret_id = google_secret_manager_secret.join_token[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${each.value}"
}

# 画面を開く先。VMを作り直してもIPを変えない。
resource "google_compute_address" "entrance" {
  name   = "${var.name}-entrance"
  region = var.region
}

resource "google_compute_instance" "server" {
  name         = "${var.name}-server"
  machine_type = var.server_machine_type
  zone         = var.zone

  boot_disk {
    initialize_params {
      image = var.node_image
      size  = 50
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.forge.id
    # 外部IPは付けない。入口はロードバランサだけにする。
  }

  metadata = {
    # OS Loginで入る。鍵をプロジェクトに配らない。
    enable-oslogin = "TRUE"
  }
  # k3sの導入・seccomp/AppArmorの配置は setup/ansible が担う
  # （roles/k3s-server, roles/node-profiles）。TerraformはVMを作るだけ。

  service_account {
    email  = google_service_account.nodes.email
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_integrity_monitoring = true
  }

  tags       = ["${var.name}-server"]
  depends_on = [google_project_service.required]

  # machine_typeの変更にはインスタンス停止が要る。付けないとterraform applyが
  # 「running中は変更できない」で失敗する(agentの定義と揃える)。
  allow_stopping_for_update = true

  lifecycle {
    ignore_changes = [
      attached_disk,
    ]
  }
}

# 生成とプレビューを動かすノード。落ちても作り直せる。
resource "google_compute_instance" "agent" {
  count        = var.agent_count
  name         = "${var.name}-agent-${count.index + 1}"
  machine_type = var.agent_machine_type
  zone         = var.zone

  allow_stopping_for_update = true

  boot_disk {
    initialize_params {
      image = var.node_image
      size  = 100
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.forge.id
    # 外向きの通信はCloud NAT経由。外部IPは付けない。
  }

  metadata = {
    enable-oslogin = "TRUE"
  }
  # k3sの導入・seccomp/AppArmorの配置・ディスクのmkfs/mountは setup/ansible が
  # 担う（roles/k3s-agent, roles/node-profiles, roles/storage）。作業領域の
  # ディスクは enable_filestore=false のとき1台目にだけ付く
  # （google_compute_attached_disk.workspaces参照）。

  service_account {
    # サーバとは別の身元。DBの接続URLはこちらからは読めない。
    email  = google_service_account.agent_nodes.email
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_integrity_monitoring = true
  }

  tags = ["${var.name}-agent"]

  lifecycle {
    ignore_changes = [
      attached_disk,
    ]
  }
}

# 生成物とプレビューの作業領域。VMを作り直しても残す。
# 1台にしか付けられない（RWO）。プレビューはこのノードへ寄せる。
# enable_filestore=true にすると、代わりにfilestore.tfの単一Filestoreを使う。
#
# 運用中のクラスタで enable_filestore を後からtrueにすると、このディスクは
# destroyされる（countを1→0にする変更のため）。先にデータをFilestore側へ
# 移行してから切り替えること。
resource "google_compute_disk" "workspaces" {
  count = var.enable_filestore ? 0 : 1
  name  = "${var.name}-workspaces"
  type  = "pd-balanced"
  zone  = var.zone
  size  = var.workspace_disk_gb
}

resource "google_compute_attached_disk" "workspaces" {
  count    = var.enable_filestore ? 0 : 1
  disk     = google_compute_disk.workspaces[0].id
  instance = var.agent_count > 0 ? google_compute_instance.agent[0].id : google_compute_instance.server.id
}

# LBの通り道とヘルスチェックからだけ受ける。インターネットからの直接の口は無い。
resource "google_compute_firewall" "from_load_balancer" {
  name    = "${var.name}-from-lb"
  network = google_compute_network.forge.id
  allow {
    protocol = "tcp"
    ports    = ["80"]
  }
  source_ranges = [var.proxy_subnet_cidr, "35.191.0.0/16", "130.211.0.0/22"]
  target_tags   = ["${var.name}-server"]
}

# 保守用の接続はIAP経由に限る。踏み台も公開ポートも作らない。
resource "google_compute_firewall" "iap_ssh" {
  name    = "${var.name}-iap-ssh"
  network = google_compute_network.forge.id
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
  source_ranges = ["35.235.240.0/20"]
  target_tags   = ["${var.name}-server", "${var.name}-agent"]
}

resource "google_compute_firewall" "internal" {
  name    = "${var.name}-internal"
  network = google_compute_network.forge.id
  allow {
    protocol = "tcp"
    ports    = ["6443", "10250", "2379-2380"]
  }
  allow {
    protocol = "udp"
    ports    = ["8472"] # flannelのVXLAN
  }
  source_tags = ["${var.name}-server", "${var.name}-agent"]
  target_tags = ["${var.name}-server", "${var.name}-agent"]
}
