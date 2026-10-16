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

With SSH bootstrap enabled, the controller installs the token on the VM automatically.
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

## Optional controller-driven VM setup

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

The controller uses SSH to install Python if missing, nftables/iproute2, its own daemon
package, `/etc/zlb/token` and `zlb-natd.service`. It enables IPv4 forwarding and starts the
service. It rechecks the installed revision, files and service every minute, and retries after
failures. Unchanged installations are not restarted. Package/configuration failure attempts to
restore the prior daemon package, unit and token. OS package operations are not rolled back.

Bootstrap currently targets Debian/Ubuntu with apt and systemd on a trusted private HTTP API
path (`lbNode.allowPrivateHTTP: true`). The controller needs root-equivalent access on that
VM, so scope the SSH key and Kubernetes Secret permissions accordingly. Keep routing, the VM's
firewall, upstream address allocation and platform lifecycle in IaC. The VM never joins Kubernetes.

For an IaC installation, derive VM addressing and SSH Secrets from VM/key resources and make
the Helm release depend on their readiness. A single IaC update can create the VM, install Helm
and let the controller complete host setup. The chart itself does not create infrastructure.

## Preprovisioned daemon

With bootstrap disabled, install Python 3.11+, nftables and iproute2 on Linux, enable IPv4
forwarding, clone `https://github.com/abexamir/zlb` and copy `zlb/` under `/opt/zlb`, install the token privately, and adapt
[`examples/systemd/zlb-natd.service`](../examples/systemd/zlb-natd.service).
Set the public interface, allowed IPs/prefixes, private API address and node CIDR explicitly.
Protect the API with a trusted private path or an external TLS/mTLS proxy. Native API TLS is
not implemented. The daemon needs NET_ADMIN/NET_RAW and persistent `/var/lib/zlb` storage.

## Routing and protocol compatibility

DNAT retains client IPs. Replies must traverse the load-balancer VM before leaving the site.
A separate egress router therefore needs appropriate return routing; routing this traffic
straight to the internet bypasses reverse NAT. Configure this in infrastructure before cutover.
Same-subnet hairpin and load balancer node-originated public-IP traffic require separate routing design.

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
