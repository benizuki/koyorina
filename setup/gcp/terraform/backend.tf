# 状態はGCSへ置く。バケット名とprefixはbackend.hclで指定する。
# backendは初期化より前に必要なため、このTerraform自身では作成しない。
terraform {
  backend "gcs" {}
}
