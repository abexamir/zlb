{{- define "zlb.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "zlb.fullname" -}}
{{- default (printf "%s-controller" .Release.Name) .Values.fullnameOverride | trunc 56 | trimSuffix "-" -}}
{{- end -}}
{{- define "zlb.selectorLabels" -}}
app: {{ include "zlb.fullname" . }}
{{- end -}}
{{- define "zlb.labels" -}}
{{ include "zlb.selectorLabels" . }}
app.kubernetes.io/name: {{ include "zlb.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{- end -}}
