variable "project_id" {
  type        = string
  description = "Koyorinaを動かすGCPプロジェクト。"
}

variable "region" {
  type    = string
  default = "asia-northeast1"
}

variable "name" {
  type        = string
  default     = "koyorina"
  description = "作るリソースの接頭辞。"
}

variable "subnet_cidr" {
  type    = string
  default = "10.10.0.0/20"
}

variable "pod_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "service_cidr" {
  type    = string
  default = "10.30.0.0/20"
}


variable "database_tier" {
  type        = string
  default     = "db-custom-1-3840"
  description = "Cloud SQLの規模。1 vCPU / 3.75GBから始める。足りなければ上げる。"
}

variable "database_disk_gb" {
  type    = number
  default = 20
  validation {
    condition     = var.database_disk_gb >= 20
    error_message = "Cloud SQLのディスクは20GB以上必要です。"
  }
}

variable "database_high_availability" {
  type        = bool
  default     = false
  description = "trueで複数ゾーン構成。費用はおよそ倍になる。運用が始まってから判断する。"
}

variable "zone" {
  type        = string
  default     = "asia-northeast1-a"
  description = "VMを置くゾーン。ディスクもここに作られる。"
}

variable "node_image" {
  type    = string
  default = "rocky-linux-cloud/rocky-linux-10-optimized-gcp"
  # RockyにはAppArmorが無く、containerd既定のプロファイルがbwrapのmountを
  # 拒む問題がそもそも起きない（Ubuntu系で専用AppArmorプロファイルが要る
  # 理由）。手元の開発クラスタも元々Rockyで動かしており、本番と揃う。
  description = "ノードのOS。k3sの動作実績で選ぶ。"
}

variable "k3s_version" {
  type        = string
  default     = "v1.31.5+k3s1"
  description = "入れるk3sの版。固定する。作り直したときに版が変わらないようにするため。"
}

variable "server_machine_type" {
  type = string
  # e2-small(2GB)ではforge_cni: ciliumのとき、k3s+Cilium+Hubble+Traefik+
  # 管理アプリでメモリが尽き、k3sサーバプロセスがOOMで落ちる。
  default     = "e2-medium"
  description = "制御と管理アプリを動かす。生成の負荷はエージェント側へ寄せる。"
}

variable "agent_machine_type" {
  type = string
  # 本番で e2-small（2GB）では足りなかった。プレビュー1本で2GiB要求・3GiB上限を取り、
  # 生成ワーカーと同居する。落ちるときは画面ビルド（vite）で落ちる。
  default     = "e2-standard-2"
  description = "生成とプレビューを動かす。"
}

variable "agent_count" {
  type    = number
  default = 1
  # 0にすると1台構成（サーバがすべて動かす）。増やすと生成の同時実行が増やせる。
  # 作業領域のディスクは1台目に付く。プレビューはその1台で動く（下のREADME参照）。
  description = "生成・プレビュー用のVMの台数。"
  validation {
    condition     = var.agent_count >= 0 && var.agent_count <= 8
    error_message = "エージェントは0〜8で指定してください。費用が跳ねるため上限を設けています。"
  }
}

variable "workspace_disk_gb" {
  type        = number
  default     = 200
  description = "生成物とプレビューの作業領域。VMを作り直しても残す。"
}

variable "domain" {
  type        = string
  description = "画面を開く名前。証明書もこの名前で取る。"
}

variable "proxy_subnet_cidr" {
  type        = string
  default     = "10.40.0.0/24"
  description = "リージョンLBが使う内部の通り道。他と重ねない。"
}

variable "cloud_armor_enabled" {
  type    = bool
  default = true
}

variable "cloud_armor_waf_preview" {
  type        = bool
  default     = true
  description = "trueの間は記録だけで落とさない。ログを見てからfalseにする。"
}

variable "cloud_armor_rate_limit_preview" {
  type    = bool
  default = true
}

variable "cloud_armor_rate_limit_count" {
  type        = number
  default     = 600
  description = "この回数を超えたら制限する。生成中は画面が短い間隔で状態を取りに来る。"
}

variable "cloud_armor_rate_limit_interval_sec" {
  type    = number
  default = 60
}

variable "cloud_armor_ban_threshold_count" {
  type    = number
  default = 1200
}

variable "cloud_armor_ban_threshold_interval_sec" {
  type    = number
  default = 60
}

variable "cloud_armor_ban_duration_sec" {
  type    = number
  default = 300
}

variable "allowed_source_ranges" {
  type        = list(string)
  default     = []
  description = "ここに範囲を入れると、そこ以外からは入れない。空なら日本国内からは入れる。"
}

# ── Filestore（RWXの作業領域） ────────────────────────────────
#
# 既定は無効。生成物とプレビューの作業領域は agent 1台に付いた200GBディスク
# （local-path、ノード固定）のまま。有効にすると、その代わりに単一の
# Filestoreインスタンスを1つ作り、setup/ansible の storage ロールが
# csi-driver-nfs でPVCごとのサブディレクトリへ分割する（PVCの数やサイズが
# 増えてもFilestore側の費用は変わらない。テナントやPVCごとにインスタンスを
# 作ることは決してしない）。
#
# 費用の目安（asia-northeast1、BASIC_HDD・1TiB）: 月おおよそ160〜200USD。
# BASIC_HDDの最小容量が1TiBのため、これより小さくはできない。
variable "enable_filestore" {
  type        = bool
  default     = false
  description = "trueにするとFilestoreを1つ作り、作業領域をRWX（複数ノードから同時マウント可能）にする。"
}

variable "filestore_tier" {
  type    = string
  default = "BASIC_HDD"
  validation {
    condition     = contains(["BASIC_HDD", "BASIC_SSD", "ZONAL"], var.filestore_tier)
    error_message = "BASIC_HDD、BASIC_SSD、ZONAL のいずれかを指定してください。"
  }
  description = "Filestoreの種別。BASIC_HDDが最小容量・最安（最小1TiB）。"
}

variable "filestore_capacity_gb" {
  type    = number
  default = 1024
  validation {
    # BASIC_HDD/ZONALの最小は1TiB。BASIC_SSDは2.5TiBが最小（利用者側で選ぶ場合は
    # tfvarsでtierとあわせて大きくすること）。
    condition     = var.filestore_capacity_gb >= 1024
    error_message = "Filestoreは最小1024GB(1TiB)からです。"
  }
  description = "Filestore共有の容量(GB)。"
}

variable "filestore_share_name" {
  type    = string
  default = "forge"
}

variable "filestore_peer_cidr" {
  type        = string
  default     = "10.60.0.0/29"
  description = "FilestoreへDIRECT_PEERINGで接続する専用の/29。他のCIDRと重ねないこと。"
}

variable "publish_join_token" {
  type        = bool
  default     = false
  description = "trueにするとk3sの参加トークンをSecret Managerへ発行する。setup/ansibleはSSH越しにトークンを直接読むので、既定では不要。"
}
