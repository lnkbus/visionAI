{{/*
공통 헬퍼.

블록마다 템플릿을 복사하지 않는다. 18개를 복사하면 보안 컨텍스트 하나 고치는 데
18곳을 고쳐야 하고, 반드시 한 곳이 빠진다 — Dockerfile을 BLOCK 인자 하나로
통일한 것과 같은 이유다.
*/}}

{{- define "visionai.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "visionai.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s" (include "visionai.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "visionai.labels" -}}
app.kubernetes.io/name: {{ include "visionai.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}

{{- define "visionai.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "visionai.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* 블록 ID: 디렉토리명 core-bus → 카탈로그 ID CORE-BUS */}}
{{- define "visionai.blockId" -}}
{{- upper . -}}
{{- end -}}

{{- define "visionai.image" -}}
{{- $tag := default .root.Chart.AppVersion .root.Values.image.tag -}}
{{- if .root.Values.image.registry -}}
{{ .root.Values.image.registry }}/{{ .root.Values.image.repository }}/{{ .name }}:{{ $tag }}
{{- else -}}
{{ .root.Values.image.repository }}/{{ .name }}:{{ $tag }}
{{- end -}}
{{- end -}}

{{/*
라이선스 Secret 이름. 기존 Secret을 지정했으면 그것을, 아니면 차트가 만든 것을 쓴다.
values에 .lic을 박아 넣으면 helm 릴리스 이력에 평문으로 남으므로 existingSecret을 권한다.
*/}}
{{- define "visionai.licenseSecretName" -}}
{{- default (printf "%s-license" (include "visionai.fullname" .)) .Values.license.existingSecret -}}
{{- end -}}

{{/*
단일 라이터 블록. 복제하면 상태가 깨진다:
  CORE-SEC — 해시 체인이 끊긴다
  CORE-LIC — 동시 설치로 .lic 파일이 깨진다
values에서 replicas를 올려도 여기서 1로 되돌린다. "성능을 올리려다 감사 로그를
못 쓰게 만드는" 사고를 설정 실수로 낼 수 있게 두지 않는다.
*/}}
{{- define "visionai.singleWriter" -}}
{{- if or (eq . "core-sec") (eq . "core-lic") (eq . "llm-sum") -}}true{{- else -}}false{{- end -}}
{{- end -}}
