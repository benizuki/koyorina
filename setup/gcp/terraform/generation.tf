# Dedicated Vertex identity: no DB secrets, registry writes or node identity.
resource "google_service_account" "gemini_workers" {
  account_id   = "${var.name}-gemini"
  display_name = "Koyorina Gemini workers"
  depends_on   = [google_project_service.required]
}

resource "google_project_iam_member" "gemini_workers" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.gemini_workers.email}"
}

# Only the trusted server VM can mint this identity's short-lived tokens.
resource "google_service_account_iam_member" "gemini_token_creator" {
  service_account_id = google_service_account.gemini_workers.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.nodes.email}"
}
