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
{{- define "zlb.clusterName" -}}
{{- printf "%s-%s" .Release.Namespace (include "zlb.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{/*
One-shot SSH bootstrap pod. It holds the VM's root-equivalent SSH key but no Kubernetes
API credentials; the long-running controller holds API credentials but no SSH key.
*/}}
{{- define "zlb.bootstrapPod" -}}
{{- $repository := required "image.repository is required" .Values.image.repository -}}
metadata:
  labels:
    {{- include "zlb.labels" . | nindent 4 }}
    app.kubernetes.io/component: bootstrap
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  enableServiceLinks: false
  {{- with .Values.imagePullSecrets }}
  imagePullSecrets:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  securityContext:
    runAsNonRoot: true
    runAsUser: 65532
    runAsGroup: 65532
    fsGroup: 65532
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: bootstrap
      image: {{ if .Values.image.digest }}{{ printf "%s@%s" $repository .Values.image.digest | quote }}{{ else }}{{ printf "%s:%s" $repository .Values.image.tag | quote }}{{ end }}
      imagePullPolicy: {{ .Values.image.pullPolicy }}
      command: [python, -m, zlb.bootstrap]
      args:
        - {{ printf "--lb-node=%s" (required "lbNode.url is required" .Values.lbNode.url) | quote }}
        - --token-file=/var/run/zlb/token
        - --key-file=/var/run/zlb-ssh/private_key
        - --known-hosts-file=/var/run/zlb-ssh/known_hosts
        - {{ printf "--ssh-host=%s" (required "bootstrap.sshHost is required" .Values.bootstrap.sshHost) | quote }}
        - {{ printf "--ssh-user=%s" .Values.bootstrap.sshUser | quote }}
        - {{ printf "--ssh-port=%v" .Values.bootstrap.sshPort | quote }}
        - {{ printf "--public-interface=%s" (required "bootstrap.publicInterface is required" .Values.bootstrap.publicInterface) | quote }}
        {{- if not .Values.bootstrap.publicIPs }}{{ fail "bootstrap.publicIPs is required" }}{{- end }}
        {{- range .Values.bootstrap.publicIPs }}
        - {{ printf "--ip=%s" . | quote }}
        {{- end }}
        - {{ printf "--node-cidr=%s" (required "nodeCIDR is required" .Values.nodeCIDR) | quote }}
        - {{ printf "--backend-port-range=%s" .Values.bootstrap.backendPortRange | quote }}
        {{- if hasPrefix "https://" .Values.lbNode.url }}
        {{- if not .Values.bootstrap.tlsSecret }}{{ fail "an https:// lbNode.url with bootstrap requires bootstrap.tlsSecret" }}{{ end }}
        - --tls-cert-file=/var/run/zlb-tls/tls.crt
        - --tls-key-file=/var/run/zlb-tls/tls.key
        {{- if .Values.bootstrap.requireClientCertificates }}
        - --tls-client-ca-file=/var/run/zlb-tls/ca.crt
        {{- end }}
        {{- else if or .Values.bootstrap.tlsSecret .Values.bootstrap.requireClientCertificates }}
        {{- fail "bootstrap.tlsSecret and requireClientCertificates need an https:// lbNode.url" }}
        {{- end }}
        {{- range .Values.bootstrap.apiAllowFrom }}
        - {{ printf "--allow-from=%s" . | quote }}
        {{- end }}
        {{- if .Values.bootstrap.allowForeignForwarding }}
        - --allow-foreign-forwarding
        {{- end }}
        {{- if .Values.bootstrap.isolatePublicIPs }}
        - --isolate-public-ips
        {{- end }}
      resources:
        {{- toYaml .Values.bootstrap.resources | nindent 8 }}
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: [ALL]
      volumeMounts:
        - {name: tmp, mountPath: /tmp}
        - {name: ssh, mountPath: /var/run/zlb-ssh, readOnly: true}
        - {name: token, mountPath: /var/run/zlb, readOnly: true}
        {{- if .Values.bootstrap.tlsSecret }}
        - {name: tls, mountPath: /var/run/zlb-tls, readOnly: true}
        {{- end }}
  volumes:
    - name: tmp
      emptyDir:
        medium: Memory
        sizeLimit: 16Mi
    - name: ssh
      secret:
        secretName: {{ required "bootstrap.existingSecret is required" .Values.bootstrap.existingSecret }}
        defaultMode: 0440
    - name: token
      secret:
        secretName: {{ required "lbNode.existingSecret is required" .Values.lbNode.existingSecret }}
        defaultMode: 0440
        items:
          - key: {{ .Values.lbNode.tokenKey | quote }}
            path: token
    {{- if .Values.bootstrap.tlsSecret }}
    - name: tls
      secret:
        secretName: {{ .Values.bootstrap.tlsSecret }}
        defaultMode: 0440
    {{- end }}
  {{- with .Values.nodeSelector }}
  nodeSelector:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  {{- with .Values.tolerations }}
  tolerations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- end -}}
