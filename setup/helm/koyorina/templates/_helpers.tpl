{{/*
Kubernetesのnamespace・Deployment名等の接頭辞。Helmのリリース名がそのまま
「以前のAPP_NAME」の役割を果たす（変数を別途持たない）。
*/}}
{{- define "koyorina.name" -}}
{{ .Release.Name }}
{{- end -}}

{{- define "koyorina.commonLabels" -}}
app.kubernetes.io/part-of: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
