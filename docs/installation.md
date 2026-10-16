# Installation

For the full illustrated walkthrough and command trace, see [installation and packet lifecycle](lifecycle.md).

Use a dedicated Linux VM reachable from Kubernetes and from clients. It must reach node
InternalIPs/NodePorts, own or receive the configured public IPs, and have a working reply
route through itself. Keep unrelated VPN and egress roles on their own router if desired.
Kubernetes must provide NodePorts and discovery/v1 EndpointSlices.

## Install through Helm

Use Helm 3.19+ (or compatible Helm 4) and the public OCI chart. Linux amd64/arm64
controller images are published at `ghcr.io/abexamir/zlb`; the stable chart package pins
its image digest. Public chart/image pulls need no GitHub or registry credentials.

Choose an explicit target cluster for every command:

```sh
export ZLB_KUBECONFIG=/absolute/path/to/target-kubeconfig
export ZLB_CONTEXT=your-target-context
kubectl --kubeconfig "$ZLB_KUBECONFIG" --context "$ZLB_CONTEXT" cluster-info
kubectl --kubeconfig "$ZLB_KUBECONFIG" --context "$ZLB_CONTEXT" \
  create namespace zlb-system --dry-run=client -o yaml |
  kubectl --kubeconfig "$ZLB_KUBECONFIG" --context "$ZLB_CONTEXT" apply -f -
```

Create the API-token Secret without putting its value in shell arguments:

```sh
ZLB_TOKEN_FILE=$(mktemp)
chmod 600 "$ZLB_TOKEN_FILE"
python3 -c 'import secrets; print(secrets.token_hex(32))' > "$ZLB_TOKEN_FILE"
kubectl --kubeconfig "$ZLB_KUBECONFIG" --context "$ZLB_CONTEXT" -n zlb-system \
  create secret generic zlb-lb-node-token --from-file=token="$ZLB_TOKEN_FILE" \
  --dry-run=client -o yaml |
  kubectl --kubeconfig "$ZLB_KUBECONFIG" --context "$ZLB_CONTEXT" apply -f -
```

With SSH bootstrap enabled, the bootstrap Job installs the token on the VM automatically.
With a preprovisioned daemon, securely install the same token at `/etc/zlb/token` on the
VM before removing the staging file. Then run `rm "$ZLB_TOKEN_FILE"`.

Download [example values](../examples/helm-values.yaml), then edit the private API endpoint,
node CIDR, public pool and reservations for your environment:

```sh
curl -fsSL https://raw.githubusercontent.com/abexamir/zlb/main/examples/helm-values.yaml \
  -o your-values.yaml
```

For a preprovisioned daemon, leave SSH bootstrap disabled and finish the
[host setup](#preprovisioned-daemon) before installing. For controller-driven setup,
create the SSH Secret and enable bootstrap as described below. Neither mode configures
your provider's routing or creates the VM.

```sh
helm upgrade --install zlb oci://ghcr.io/abexamir/charts/zlb --version 0.2.0 \
  --namespace zlb-system --create-namespace \
  --kubeconfig "$ZLB_KUBECONFIG" --kube-context "$ZLB_CONTEXT" \
  -f your-values.yaml --wait --timeout 15m
kubectl --kubeconfig "$ZLB_KUBECONFIG" --context "$ZLB_CONTEXT" -n zlb-system \
  get deployments
```

The chart deploys one controller with Recreate strategy, RBAC and pool configuration. Readiness
requires a successful load balancer node apply and Kubernetes reconciliation. It does not imply the
application itself is healthy. Test each published port from an external client.

## Optional SSH bootstrap of the VM

Set these values in addition to the load balancer node URL and token Secret:

```yaml
bootstrap:
  enabled: true
  existingSecret: zlb-ssh
  sshHost: 10.0.0.3
  sshUser: root
  sshPort: 22
  publicInterface: eth0
  publicIPs: [198.51.100.10/32]
```

Create the Secret referenced by `bootstrap.existingSecret` before the Helm install:

```sh
kubectl --kubeconfig "$ZLB_KUBECONFIG" --context "$ZLB_CONTEXT" -n zlb-system \
  create secret generic zlb-ssh \
  --from-file=private_key=/secure/path/to/dedicated-private-key \
  --from-file=known_hosts=/secure/path/to/trusted-known-hosts
```

The SSH Secret must contain `private_key` and `known_hosts`. Generate a dedicated key for this
VM and obtain its host public key from your provisioning system. Host verification is strict;
there is no automatic trust-on-first-use. Non-root users need passwordless sudo.

Bootstrap runs as a Helm `pre-install`/`pre-upgrade` hook Job, `<release>-controller-bootstrap`.
It uses SSH to install Python if missing, nftables/iproute2, the daemon package,
`/etc/zlb/token` and `zlb-natd.service`. It enables IPv4 forwarding and starts the service.
Unchanged installations are not restarted. Package or configuration failures attempt to
restore the prior daemon package, unit and token. OS package operations are not rolled back.
A failed Job fails the Helm install or upgrade and stays for inspection
(`kubectl logs job/<release>-controller-bootstrap`).

The SSH key is root-equivalent on the VM, so only the bootstrap pod mounts it. That pod has
no Kubernetes API token. The long-running controller has API access but never sees the key.
To repair drift between upgrades (for example after rebuilding the VM), set
`bootstrap.schedule` to a cron expression. A CronJob then reruns the same one-shot pod.

On `helm upgrade` and `helm rollback`, an earlier `-quiesce` hook Job stops every other
bootstrap writer before the bootstrap hook runs. That covers the drift-repair CronJob and
its running Jobs, and also a controller from before this split (0.4.0 and earlier), which
ran SSH bootstrap itself. The quiesce Job has namespace-scoped API access and no SSH key.
It waits until those pods are gone, so no older payload can reinstall the daemon after the
hook. The daemon is therefore updated before the new controller starts. The upgrade then
restores the controller to one replica and resumes the CronJob. If the bootstrap hook
fails, the upgrade fails and the CronJob stays suspended. A legacy controller that the
quiesce Job scaled down also stays down until a later upgrade or `helm rollback` succeeds.
Forwarding rules keep serving meanwhile.

Bootstrap currently targets Debian/Ubuntu with apt and systemd. Scope the SSH key and the namespace's Secret read
permissions accordingly. Keep routing, the VM's firewall, upstream address allocation and
platform lifecycle in IaC. The VM never joins Kubernetes.

For an IaC installation, derive VM addressing and SSH Secrets from VM/key resources and make
the Helm release depend on their readiness. A single IaC update can create the VM, then run
Helm, whose bootstrap hook completes host setup. The chart itself does not create infrastructure.

## Preprovisioned daemon

With bootstrap disabled, install Python 3.11+, nftables and iproute2 on Linux, enable IPv4
forwarding, clone `https://github.com/abexamir/zlb` and copy `zlb/` under `/opt/zlb`.
Create the `zlb-natd` system user, install the token as `root:zlb-natd` mode 0640, and
adapt [`examples/systemd/zlb-natd.service`](../examples/systemd/zlb-natd.service); its
header shows the commands. Set the public interface, allowed IPs/prefixes, private API
address and node CIDR explicitly. Protect the API with HTTPS (see below) or a trusted
private path. The daemon runs unprivileged with only `CAP_NET_ADMIN` and needs persistent
`/var/lib/zlb` storage.

## Load balancer node host protection

IPv4 forwarding makes the VM a router. Without a default-deny forward policy, a neighbour
on the public network could route packets through it straight into the node CIDR. The
daemon therefore drops new or invalid connections arriving on `--public-interface` unless
they were DNATed (zlb listeners or your own DNAT rules). Use `--allow-foreign-forwarding`
(`bootstrap.allowForeignForwarding`) only if the VM must also route other public traffic.

Services on the VM itself, such as sshd, also answer on the public IPs. Add
`--isolate-public-ips` (`bootstrap.isolatePublicIPs`) to accept only listeners, replies and
ping on those addresses. Do not enable it if you administer the VM through a public IP.
Your own firewall policy still applies on top of both.

## Securing the daemon API

The bearer token travels with every request, so prefer HTTPS whenever the path between the
cluster and the VM is not fully trusted. The daemon serves TLS 1.2+ natively:

- `--tls-cert` and `--tls-key`: the server certificate must carry an IP SAN for the address
  in `lbNode.url`. Give the controller the issuing CA through `lbNode.caConfigMap`.
- `--tls-client-ca`: additionally require client certificates signed by exactly this CA
  (mutual TLS; the system trust store is not consulted). Give the controller its certificate
  through `lbNode.clientCertSecret`, a `kubernetes.io/tls` Secret.
- `--allow-from CIDR` (repeatable): close connections from other sources before reading
  anything. Your node CIDR is a good start when pods reach the VM from node addresses.
- `--max-connections` (default 16) caps concurrent connections, and each request must
  arrive within 30 seconds, so unauthenticated clients cannot exhaust the daemon.

With SSH bootstrap, use an `https://` `lbNode.url` and set `bootstrap.tlsSecret` to a
Secret holding `tls.crt` and `tls.key`. Add `ca.crt` and `bootstrap.requireClientCertificates:
true` for mutual TLS, and list sources in `bootstrap.apiAllowFrom`:

```yaml
lbNode:
  url: https://10.0.0.3:9808
  existingSecret: zlb-lb-node-token
  caConfigMap: zlb-lb-node-ca
  clientCertSecret: zlb-controller-client
bootstrap:
  tlsSecret: zlb-lb-node-tls
  requireClientCertificates: true
  apiAllowFrom: [10.0.0.0/24]
```

Plain HTTP still requires `lbNode.allowPrivateHTTP: true` and should be limited to a
trusted private network.

## Routing and protocol compatibility

DNAT retains client IPs. Replies must traverse the load-balancer VM before leaving the site.
A separate egress router therefore needs appropriate return routing; routing this traffic
straight to the internet bypasses reverse NAT. Configure this in infrastructure before cutover.
Load balancer node-originated public-IP traffic is not forwarded; test from another host.

### In-cluster clients (hairpin)

A Pod or node calling a public IP reaches the load balancer node with a source address
inside the cluster, usually its node's address after CNI SNAT. The chosen backend node then
replies straight to that address, not through the load balancer node, and the client drops a
reply that does not come from the public IP it called. Pass `--hairpin-snat-from` with those
source CIDRs (`bootstrap.hairpinSnatFrom`, typically your node CIDR, plus the Pod CIDR if
your CNI does not SNAT Pod egress) to masquerade such connections as the load balancer
node's address on the node network:

```sh
python3 -m zlb.natd ... --hairpin-snat-from 10.0.0.0/24
```

Only zlb-DNATed connections from those sources are masqueraded, so external clients keep
their address. The workload sees the load balancer node instead of the in-cluster client,
and `loadBalancerSourceRanges` still applies to the original source.

TCP, UDP and SCTP may share numeric ports. The load balancer node kernel needs conntrack/NAT support for
each protocol; Kubernetes and its service datapath must support the corresponding NodePort
translation. Basic SCTP associations are supported; multihoming is not claimed.
[Cilium's SCTP documentation](https://docs.cilium.io/en/stable/configuration/sctp/) describes
NodePort port-rewriting limitations with kube-proxy replacement. Load balancer node SCTP support cannot
supply a missing CNI capability.

Use [operations](operations.md) to verify traffic and [configuration](configuration.md) for
pool reservations, source ranges and daemon ownership behavior.

## Uninstall

Delete or convert managed LoadBalancer Services while the controller and LB are still
available, then wait for zlb cleanup finalizers to disappear. After that:

```sh
helm uninstall zlb --namespace zlb-system --kubeconfig "$ZLB_KUBECONFIG" \
  --kube-context "$ZLB_CONTEXT" --wait
# On the explicit load balancer node:
sudo systemctl disable --now zlb-natd.service
```

Uninstalling Helm alone leaves the external daemon installed. Preserve the address journal
until cleanup has completed. See [operations](operations.md) for failure recovery.
