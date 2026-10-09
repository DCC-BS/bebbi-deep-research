{{- define "bdr.name" -}}{{ .Chart.Name }}{{- end -}}
{{- define "bdr.fullname" -}}{{ default .Chart.Name .Values.fullnameOverride }}{{- end -}}
{{- define "bdr.labels" -}}
app.kubernetes.io/name: {{ include "bdr.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
{{- define "bdr.selector" -}}
app.kubernetes.io/name: {{ include "bdr.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{- define "bdr.presetsConfigMap" -}}
{{- default (printf "%s-presets" (include "bdr.fullname" .)) .Values.presets.existingConfigMap -}}
{{- end -}}
