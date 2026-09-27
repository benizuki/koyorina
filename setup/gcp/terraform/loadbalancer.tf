/** 入口。リージョンの外部アプリケーションLBで受け、Cloud Armorで守る。
 *
 * VMに直接グローバルIPを付けない。受けるのはLBだけにして、
 * 国・WAF・レート制限をそこで効かせる。TLSもLBで終端する。
 */

# L7のLBが使う内部の通り道。リージョンに1つだけ作る。
resource "google_compute_subnetwork" "proxy" {
  name          = "${var.name}-proxy"
  ip_cidr_range = var.proxy_subnet_cidr
  region        = var.region
  network       = google_compute_network.forge.id
  purpose       = "REGIONAL_MANAGED_PROXY"
  role          = "ACTIVE"
}

# サーバVMをLBの宛先として束ねる。台数は1台のまま。
resource "google_compute_instance_group" "server" {
  name      = "${var.name}-server-group"
  zone      = var.zone
  instances = [google_compute_instance.server.self_link]

  named_port {
    name = "http"
    port = 80
  }
}

resource "google_compute_region_health_check" "server" {
  name               = "${var.name}-health"
  region             = var.region
  check_interval_sec = 10
  timeout_sec        = 5
  healthy_threshold  = 1
  # 落ちたと判断するまで少し待つ。生成や再起動で一時的に重くなることがある。
  unhealthy_threshold = 3

  http_health_check {
    port         = 80
    request_path = "/healthz"
    host         = var.domain # IngressのHostルーティングとTrustedHostMiddlewareに合わせる
  }
}

resource "google_compute_region_backend_service" "server" {
  name                  = "${var.name}-backend"
  region                = var.region
  protocol              = "HTTP"
  port_name             = "http"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  timeout_sec           = 120 # 生成の待ち時間が長い経路がある
  health_checks         = [google_compute_region_health_check.server.id]

  backend {
    group           = google_compute_instance_group.server.id
    balancing_mode  = "UTILIZATION"
    capacity_scaler = 1.0
  }

  security_policy = var.cloud_armor_enabled ? google_compute_region_security_policy.app_lb[0].id : null

  log_config {
    enable      = true
    sample_rate = 1.0
  }
}

resource "google_compute_region_url_map" "server" {
  name            = "${var.name}-urlmap"
  region          = var.region
  default_service = google_compute_region_backend_service.server.id
}

# 証明書。Certificate Managerで取り、更新も任せる。
resource "google_certificate_manager_dns_authorization" "forge" {
  name        = "${var.name}-dns-auth-${substr(sha256(var.domain), 0, 8)}"
  location    = var.region
  domain      = var.domain
  description = "Koyorinaの証明書を取るためのDNS確認"

  lifecycle {
    create_before_destroy = true
  }
}

resource "google_certificate_manager_certificate" "forge" {
  name     = "${var.name}-cert-${substr(sha256(var.domain), 0, 8)}"
  location = var.region

  managed {
    domains            = [var.domain]
    dns_authorizations = [google_certificate_manager_dns_authorization.forge.id]
  }

  lifecycle {
    create_before_destroy = true
  }
}

# TLS 1.3以上だけを許可する。最小TLS 1.3にはRESTRICTEDが必要。
resource "google_compute_region_ssl_policy" "server" {
  name            = "${var.name}-ssl-policy"
  region          = var.region
  profile         = "RESTRICTED"
  min_tls_version = "TLS_1_3"

  depends_on = [google_project_service.required]
}

resource "google_compute_region_target_https_proxy" "server" {
  name       = "${var.name}-https-proxy"
  region     = var.region
  url_map    = google_compute_region_url_map.server.id
  ssl_policy = google_compute_region_ssl_policy.server.id
  certificate_manager_certificates = [
    google_certificate_manager_certificate.forge.id
  ]
}

resource "google_compute_forwarding_rule" "https" {
  name                  = "${var.name}-https"
  region                = var.region
  ip_address            = google_compute_address.entrance.address
  ip_protocol           = "TCP"
  port_range            = "443"
  load_balancing_scheme = "EXTERNAL_MANAGED"
  target                = google_compute_region_target_https_proxy.server.id
  network               = google_compute_network.forge.id
  depends_on            = [google_compute_subnetwork.proxy]
}
