{{/*
Expand the name of the chart.
*/}}
{{- define "inframind.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified app name. Truncated to 63 characters to comply with Kubernetes DNS naming
specifications (RFC 1123).
*/}}
{{- define "inframind.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Chart label, recomputed per resource.
*/}}
{{- define "inframind.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels — every templated resource gets these.
*/}}
{{- define "inframind.labels" -}}
helm.sh/chart: {{ include "inframind.chart" . }}
{{ include "inframind.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels — the immutable subset used by selectors and matchers.
*/}}
{{- define "inframind.selectorLabels" -}}
app.kubernetes.io/name: {{ include "inframind.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The ServiceAccount name.
*/}}
{{- define "inframind.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (printf "%s-sa" (include "inframind.fullname" .)) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
The Secret name the Deployment references for envFrom. Empty value falls back to the chart's
own Secret resource; non-empty uses the operator-provided one.
*/}}
{{- define "inframind.secretName" -}}
{{- if .Values.existingSecret -}}
{{- .Values.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "inframind.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
The full image reference. Always rendered as `<repository>:<tag>`; Helm is happy with quoted
strings and so is the kubelet.
*/}}
{{- define "inframind.image" -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- end -}}
