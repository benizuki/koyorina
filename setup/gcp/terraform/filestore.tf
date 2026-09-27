/** 作業領域をRWXにする（任意）。
 *
 * 既定は無効。生成物とプレビューの作業領域は agent 1台に付いたディスク
 * （cluster.tf の google_compute_disk.workspaces、local-pathでノード固定）の
 * ままにする。有効にすると、その代わりにFilestoreを1つだけ作る。
 *
 * インスタンスは常にちょうど1つ。テナントやPVCの数に連動させない。
 * setup/ansible の storage ロールが csi-driver-nfs でこの1つの共有を
 * PVCごとのサブディレクトリへ分割するので、PVCが何個・何Giになっても
 * Filestore側の費用は変わらない（2GiBの利用者PVCも、ただのディレクトリになる）。
 */

resource "google_project_service" "filestore" {
  count              = var.enable_filestore ? 1 : 0
  project            = var.project_id
  service            = "file.googleapis.com"
  disable_on_destroy = false
}

# database.tf が Cloud SQL 用に確保している /16（VPC Peering）とは経路を分ける。
# 同じservicenetworking接続を共有すると、割り当て済み範囲の変更で
# "Cannot modify allocated ranges" に当たりやすいため、DIRECT_PEERINGで
# 専用の/29を使う。
resource "google_filestore_instance" "workspaces" {
  count    = var.enable_filestore ? 1 : 0
  name     = "${var.name}-workspaces"
  location = var.zone
  tier     = var.filestore_tier

  file_shares {
    name        = var.filestore_share_name
    capacity_gb = var.filestore_capacity_gb
  }

  networks {
    # Filestore APIはネットワークをフルパス(self_link)ではなく短い名前で
    # 扱う。.idを渡すと、apply後の次回planで必ずdiffが出て強制的に
    # 作り直しになる(Filestore側が読み出し時に短い名前へ正規化するため)。
    network           = google_compute_network.forge.name
    modes             = ["MODE_IPV4"]
    connect_mode      = "DIRECT_PEERING"
    reserved_ip_range = var.filestore_peer_cidr
  }

  depends_on = [google_project_service.required, google_project_service.filestore]
}
