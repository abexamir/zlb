# zlb Helm chart

Installs one controller, ServiceAccount, cluster RBAC and pool ConfigMap. The external Linux
VM must have working network routes. Either provision `zlb-natd` yourself or enable optional
controller-driven SSH setup; see [installation](../../docs/installation.md).

```sh
helm upgrade --install zlb oci://ghcr.io/abexamir/charts/zlb --version 0.2.0 \
  --namespace zlb-system --create-namespace \
  --kubeconfig /absolute/path/to/target-kubeconfig -f your-values.yaml --wait
```

The stable chart defaults to the public `ghcr.io/abexamir/zlb` image. Published chart
packages pin the matching image digest; no registry login is needed for public pulls.
Use `helm show values oci://ghcr.io/abexamir/charts/zlb --version 0.2.0` to inspect defaults.

Start with [example values](../../examples/helm-values.yaml). Set `image.repository`,
`image.tag` or `image.digest`, `lbNode.url`, `lbNode.existingSecret`, `nodeCIDR` and `pools`.
The existing Secret must contain the same token installed on the VM, under `token` by default.
Use `lbNode.tokenKey` for another key. Set `lbNode.caConfigMap` for a private HTTPS CA
(ConfigMap key `ca.crt`). HTTP requires explicit `lbNode.allowPrivateHTTP: true`.

One controller replica and Recreate strategy are intentional: each push replaces the complete
external load balancer node state. Namespaced installations have separate cluster-RBAC names; they still
must not control the same load balancer node. The chart does not create application NetworkPolicies,
provision VMs, or automatically expose workloads.

Before uninstalling, remove or convert its LoadBalancer Services while the controller and
VM are healthy, allowing cleanup finalizers to complete. See [operations](../../docs/operations.md).

Set `bootstrap.enabled`, `bootstrap.existingSecret`, `bootstrap.sshHost`,
`bootstrap.publicInterface` and `bootstrap.publicIPs` for SSH provisioning. The Secret holds
`private_key` and `known_hosts`; use a dedicated VM key and a trusted provisioned host key.
A `pre-install`/`pre-upgrade` hook Job, not the controller, mounts that root-equivalent key.
The Job has no Kubernetes API token. Set `bootstrap.schedule` to add a drift-repair CronJob.
On upgrade and rollback a preceding quiesce hook stops that CronJob and any controller
from 0.4.0 or earlier that bootstraps by itself, so no older payload runs after the
bootstrap hook.

With `rbac.restrictServiceWrites` (default `true`) on Kubernetes 1.30+, a
ValidatingAdmissionPolicy limits the controller's Service updates to its own finalizer.

Chart 0.2.0 / application 0.4.0 names the public-facing forwarding machine the
**load balancer node**. Update the former `gateway` values block to `lbNode` and
the controller endpoint flag to `--lb-node` when upgrading. The old values key is
rejected. The historical `zlb.zarrino.tech/gateway` LoadBalancer class remains
accepted for compatibility with existing Services; it is not the node endpoint setting.
