# zlb Helm chart

Installs the zlb controller (one replica, Recreate strategy), its ServiceAccount, cluster
RBAC and pool ConfigMap. On Kubernetes 1.30+ a ValidatingAdmissionPolicy
(`rbac.restrictServiceWrites`) limits the controller's Service writes to its own finalizer
and `zlb.zarrino.tech/published-ports` annotation. With `bootstrap.enabled`, hook Jobs
install and upgrade the daemon on the VM over SSH. The chart never creates the VM, Secrets,
routes or NetworkPolicies.

See the [operator guide](../../docs/guide.md#install) for the install command, Secrets,
TLS, bootstrap and uninstall. Start from the
[example values](../../examples/helm-values.yaml); [values.yaml](values.yaml) lists every value.

## Upgrade notes

Upgrade order and version skew are described in
[the operator guide](../../docs/guide.md#restart-and-upgrade).

**Chart 0.3.0 / application 0.5.0**

- SSH bootstrap moved out of the controller into hook Jobs. Before the bootstrap hook
  upgrades the daemon, a quiesce hook suspends the CronJob, deletes its Jobs and scales down
  a 0.4.0 controller that runs bootstrap itself; no manual steps are needed.
- The controller sends wire version 3 and needs a 0.5.0 daemon. A preprovisioned daemon must
  be upgraded first, and now runs as the `zlb-natd` user: follow the guide's upgrade steps.
- The daemon keeps forwarding across crashes and managed restarts.
- With bootstrap, `lbNode.url` must be `http(s)://IPv4:PORT`.
- The controller needs `watch` on Services, Nodes and EndpointSlices (the chart's RBAC
  includes it) and writes the `zlb.zarrino.tech/published-ports` annotation.
- New values: `bootstrap.schedule`, `bootstrap.backendPortRange`, `bootstrap.tlsSecret`,
  `bootstrap.requireClientCertificates`, `bootstrap.apiAllowFrom`,
  `bootstrap.allowForeignForwarding`, `bootstrap.isolatePublicIPs`,
  `bootstrap.hairpinSnatFrom`, `bootstrap.resources`, `lbNode.clientCertSecret` and
  `rbac.restrictServiceWrites`.

**Chart 0.2.0 / application 0.4.0**

The public-facing forwarding machine is called the **load balancer node**. Rename the former
`gateway` values block to `lbNode` and the controller endpoint flag to `--lb-node`; the old
values key is rejected. The historical `zlb.zarrino.tech/gateway` LoadBalancer class remains
accepted for existing Services; it is not the node endpoint setting.

Optional `bootstrap.routes` manages explicit main-table IPv4 routes on the LB; see
[LB routes](../../docs/guide.md#optional-lb-routes). The default is disabled.
