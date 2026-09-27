/** Cloud Armor。LBの手前で落とす。
 *
 * 社内向けの基盤なので、国の制限を先に効かせる。WAFは既成のルールを使い、
 * 誤検知が怖いものはpreview（記録だけ）で始めて、ログを見てから有効にする。
 */
resource "google_compute_region_security_policy" "app_lb" {
  count = var.cloud_armor_enabled ? 1 : 0

  name        = "${var.name}-lb-policy"
  description = "Koyorinaのリージョンロードバランサを守る。"
  region      = var.region
  type        = "CLOUD_ARMOR"

  advanced_options_config {
    json_parsing = "STANDARD"
    log_level    = "VERBOSE"
  }

  rules {
    action      = "allow"
    priority    = 100
    description = "国の制限より先に、LBのヘルスチェックを通す。"

    match {
      expr {
        expression = "inIpRange(origin.ip, '35.191.0.0/16') || inIpRange(origin.ip, '130.211.0.0/22')"
      }
    }
  }

  # 社内の固定IPやVPNの範囲が分かっているなら、そこだけに絞る。
  dynamic "rules" {
    for_each = length(var.allowed_source_ranges) > 0 ? [1] : []
    content {
      action      = "deny(403)"
      priority    = 800
      description = "指定した範囲以外からは入れない。"

      match {
        expr {
          expression = join(" && ", [
            for range in var.allowed_source_ranges : "!inIpRange(origin.ip, '${range}')"
          ])
        }
      }
    }
  }

  rules {
    action      = "deny(403)"
    priority    = 900
    description = "日本国内からの通信だけを通す。"

    match {
      expr {
        expression = "origin.region_code != 'JP'"
      }
    }
  }

  rules {
    action      = "deny(403)"
    priority    = 1000
    preview     = var.cloud_armor_waf_preview
    description = "SQLインジェクションを落とす（v422 stable）。"

    match {
      expr {
        expression = "evaluatePreconfiguredWaf('sqli-v422-stable', {'sensitivity': 1})"
      }
    }
  }

  rules {
    action      = "deny(403)"
    priority    = 1010
    preview     = var.cloud_armor_waf_preview
    description = "クロスサイトスクリプティングを落とす（v422 stable）。"

    match {
      expr {
        expression = "evaluatePreconfiguredWaf('xss-v422-stable', {'sensitivity': 1})"
      }
    }
  }

  rules {
    action   = "deny(403)"
    priority = 1020
    # 生成物のプレビューを通す経路があるため、まず記録だけにする。
    preview     = true
    description = "プロトコル攻撃の検知を記録する（v422 stable）。"

    match {
      expr {
        expression = "evaluatePreconfiguredWaf('protocolattack-v422-stable', {'sensitivity': 1})"
      }
    }
  }

  rules {
    action      = "deny(403)"
    priority    = 1030
    preview     = true
    description = "セッション固定の検知を記録する（v422 stable）。"

    match {
      expr {
        expression = "evaluatePreconfiguredWaf('sessionfixation-v422-stable', {'sensitivity': 1})"
      }
    }
  }

  rules {
    action      = "deny(403)"
    priority    = 1040
    preview     = var.cloud_armor_waf_preview
    description = "遠隔からのコード実行を落とす（v422 stable）。"

    match {
      expr {
        expression = "evaluatePreconfiguredWaf('rce-v422-stable', {'sensitivity': 1})"
      }
    }
  }

  rules {
    action      = "rate_based_ban"
    priority    = 2000
    preview     = var.cloud_armor_rate_limit_preview
    description = "同じ送信元からの過剰な要求を制限し、一定時間締め出す。"

    match {
      versioned_expr = "SRC_IPS_V1"

      config {
        src_ip_ranges = ["*"]
      }
    }

    rate_limit_options {
      conform_action   = "allow"
      exceed_action    = "deny(429)"
      enforce_on_key   = "IP"
      ban_duration_sec = var.cloud_armor_ban_duration_sec

      rate_limit_threshold {
        count        = var.cloud_armor_rate_limit_count
        interval_sec = var.cloud_armor_rate_limit_interval_sec
      }

      ban_threshold {
        count        = var.cloud_armor_ban_threshold_count
        interval_sec = var.cloud_armor_ban_threshold_interval_sec
      }
    }
  }

  rules {
    action      = "allow"
    priority    = 2147483647
    description = "どのルールにも当たらなかった通信を通す。"

    match {
      versioned_expr = "SRC_IPS_V1"

      config {
        src_ip_ranges = ["*"]
      }
    }
  }

  depends_on = [google_project_service.required]
}
