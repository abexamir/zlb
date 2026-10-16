# zlb operator guide

How to install, secure, configure, operate and troubleshoot zlb. For how it works inside,
see [internals](internals.md).

Examples use documentation addresses; replace them with yours. The example topology: daemon
API `10.0.0.3:9808` on the VM, public interface `eth0`, node CIDR `10.0.0.0/24`, public IP
`198.51.100.10`. Every command names its target cluster explicitly:

```sh
export ZLB_KUBECONFIG=/absolute/path/to/target-kubeconfig
export ZLB_CONTEXT=your-target-context
export ZLB_LB_NODE_SSH=root@10.0.0.3
alias k='kubectl --kubeconfig "$ZLB_KUBECONFIG" --context "$ZLB_CONTEXT"'
```

- [Requirements](#requirements)
- [Install](#install)
- [Secure the load balancer node](#secure-the-load-balancer-node)
- [Networking](#networking)
- [Configure pools and Services](#configure-pools-and-services)
- [Command-line flags](#command-line-flags)
- [Operate](#operate)
- [Troubleshoot](#troubleshoot)

## Requirements

A dedicated Linux VM (the **load balancer node**) that receives or owns the public IPs,
reaches node InternalIPs and NodePorts, and carries the replies: return traffic must pass
through it. It never joins Kubernetes.

| Component | Requirement | Verified in CI | Verified manually |
| --- | --- | --- | --- |
| Kubernetes | NodePorts and discovery/v1 EndpointSlices (1.21+). 1.30+ adds `ipMode: Proxy` and the `rbac.restrictServiceWrites` admission policy; older API servers drop or skip both. | 1.34, 1.37 (kind, end to end) | API servers 1.29, 1.34 |
| Helm | 3.19+ or a compatible Helm 4 | 3.19 | |
| Controller | the published image, or Python 3.11+ | Python 3.11, 3.13, 3.14 | |
| Load balancer node | Linux with IPv4 forwarding, nftables, iproute2, Python 3.11+; systemd recommended | Debian 13, nftables 1.1 | Ubuntu 24.04, nftables 1.0.9 |
| SSH bootstrap target | Debian 12+ or Ubuntu 24.04+ (apt, systemd, Python 3.11+) | remote script on Debian 13, systemd emulated | the same on Ubuntu 24.04 |

The daemon checks every candidate table with `nft --check` before changing anything. A
kernel or nftables build that lacks a feature zlb uses is therefore refused before any change:
at startup for the base table, or on the first push that needs the feature, which answers
503 and keeps the old table. SCTP also needs SCTP conntrack/NAT in the VM's kernel and your CNI.

## Install

### 1. Namespace and API token

```sh
k create namespace zlb-system --dry-run=client -o yaml | k apply -f -
ZLB_TOKEN_FILE=$(mktemp) && chmod 600 "$ZLB_TOKEN_FILE"
python3 -c 'import secrets; print(secrets.token_hex(32))' > "$ZLB_TOKEN_FILE"
k -n zlb-system create secret generic zlb-lb-node-token --from-file=token="$ZLB_TOKEN_FILE" \
  --dry-run=client -o yaml | k apply -f -
```

SSH bootstrap copies this token to the VM. For a preprovisioned daemon, install the same
token as `/etc/zlb/token` first. Then `rm "$ZLB_TOKEN_FILE"`.

### 2. Values and Helm

Start from the [example values](../examples/helm-values.yaml): set `lbNode.url` (with its
port), `nodeCIDR` and `pools`. Then:

```sh
helm upgrade --install zlb oci://ghcr.io/abexamir/charts/zlb --version 0.3.0 \
  --namespace zlb-system --create-namespace \
  --kubeconfig "$ZLB_KUBECONFIG" --kube-context "$ZLB_CONTEXT" \
  -f your-values.yaml --wait --timeout 15m
```

Keep the 15-minute timeout; the bootstrap hook installs packages on the VM. The chart runs
one controller replica with Recreate strategy: two controllers must never drive one daemon.
The controller is Ready only after a tick that the daemon acknowledged; that says nothing
about your application, so test each published port from an outside client.

Published charts pin the image by digest, which wins over `image.tag`. For your own image
set `image.repository`, `image.tag` and `image.digest: ""`.

### 3a. SSH bootstrap (optional)

Let the chart install and upgrade the daemon on one explicit VM:

```yaml
bootstrap:
  enabled: true
  existingSecret: zlb-ssh
  sshHost: 10.0.0.3          # IPv4 literal
  sshUser: root              # others need passwordless sudo
  publicInterface: eth0
  publicIPs: [198.51.100.10/32]
```

```sh
k -n zlb-system create secret generic zlb-ssh \
  --from-file=private_key=/secure/path/to/dedicated-private-key \
  --from-file=known_hosts=/secure/path/to/trusted-known-hosts
```

Use a dedicated key and the host key from your provisioning system; there is no
trust-on-first-use. With bootstrap, `lbNode.url` must be `http(s)://IPv4:PORT` on that VM.

A `pre-install`/`pre-upgrade`/`pre-rollback` hook Job installs Python if missing,
nftables, iproute2, the `zlb-natd` user, the daemon package, token, TLS files and
`zlb-natd.service`, enables IPv4 forwarding and starts the service. Unchanged installs are
not touched; a token-only change is written without a restart. Before it runs on upgrade or
rollback, a quiesce hook suspends the optional drift-repair CronJob (`bootstrap.schedule`)
and scales down any 0.4.0-or-older controller that bootstrapped by itself.

If anything fails, the hook restores the previous daemon without interrupting forwarding
([details](internals.md#ssh-bootstrap)) and the Helm operation fails. The Job's log only
gives the exit code, because remote output can contain secrets (255 means SSH could not
connect, authenticate or verify the host key); look on the VM with
`journalctl -u zlb-natd` and `systemctl status zlb-natd`.

Only the bootstrap pod mounts the root-equivalent SSH key, and it has no Kubernetes API
token; the controller has API access and no key. Creating the VM, routing and firewall
stay with your infrastructure tooling.

### 3b. Preprovisioned daemon

Install Python 3.11+, nftables and iproute2, copy the `zlb/` package to `/opt/zlb/zlb`, and
follow the header of [the example unit](../examples/systemd/zlb-natd.service): it creates
the user, installs the token (`root:zlb-natd`, 0640), persists IPv4 forwarding and enables
the unit. ExecStopPost needs `--listen` and the same `--ip`, `--public-interface`,
`--node-cidr`, `--table` and `--state-dir` as ExecStart. Keep `/var/lib/zlb` persistent.

## Secure the load balancer node

### Daemon API

The bearer token travels with every request; use HTTPS unless the path is fully private.

| Daemon flag | Chart value | Effect |
| --- | --- | --- |
| `--tls-cert`, `--tls-key` | `bootstrap.tlsSecret` (`tls.crt`, `tls.key`) | TLS 1.2+. The certificate needs an IP SAN for the `lbNode.url` host. |
| `--tls-client-ca` | `ca.crt` in `bootstrap.tlsSecret` and `bootstrap.requireClientCertificates: true` | Require client certificates from exactly this CA. |
| `--allow-from CIDR` | `bootstrap.apiAllowFrom` | Close other connections before reading anything. |
| `--max-connections` (16) | | Cap concurrent connections; each request must arrive within 30 s. |

On the controller side, `lbNode.caConfigMap` (key `ca.crt`) is the CA that signed the
daemon's certificate, and `lbNode.clientCertSecret` (a `kubernetes.io/tls` Secret) is the
controller's client certificate. Plain HTTP needs `lbNode.allowPrivateHTTP: true`.

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

The daemon only forwards to backend ports in `--backend-port-range`
(`bootstrap.backendPortRange`, default `30000-32767`; match your NodePort range), so a
stolen token cannot publish SSH, the kubelet or etcd on a node.

### Host protection

IPv4 forwarding makes the VM a router. The daemon therefore drops new or invalid connections
arriving on `--public-interface` that were not DNATed, so a neighbour cannot route into the
node network through it. Disable that only if the VM must route other public traffic
(`--allow-foreign-forwarding`, `bootstrap.allowForeignForwarding`).

Local services such as sshd also answer on the public IPs. `--isolate-public-ips`
(`bootstrap.isolatePublicIPs`) accepts only listeners, replies and ping there; leave it off
if you administer the VM through a public IP.

### Firewall baseline

zlb owns only `table ip zlb`. Your firewall still applies, with three rules of thumb:

- Listener traffic is DNATed and **forwarded**; it never reaches your input chain. An accept
  in zlb's table does not stop other tables, so every forward chain with a drop policy
  (yours, firewalld, ufw, Docker) must accept DNATed traffic to the node CIDR and replies.
- Open the API port only to where the controller connects from, and SSH only to admins.
- Never load a ruleset starting with `flush ruleset`: it deletes zlb's table, and
  forwarding stops until the next controller tick reinstalls it.

A minimal default-deny baseline:

```nft
table inet site_filter {
  chain input {
    type filter hook input priority 0; policy drop;
    ct state established,related accept
    iif lo accept
    icmp type echo-request accept
    ip saddr 192.0.2.0/24 tcp dport 22 accept
    ip saddr 10.0.0.0/24 tcp dport 9808 accept
  }
  chain forward {
    type filter hook forward priority 0; policy drop;
    ct state established,related accept
    ct status dnat ip daddr 10.0.0.0/24 accept
  }
}
```

## Networking

**Return path.** DNAT keeps the client's address, so replies from the nodes must route back
through the VM for reverse translation. A separate egress router needs return routing for
NodePort replies; see the [routing example](internals.md#return-routing-example). Under
`externalTrafficPolicy: Cluster` the node may SNAT again; use `Local` when workloads need the
client address.

**In-cluster clients (hairpin).** A Pod or node calling a public IP gets the reply straight
from the backend node, not from the public IP, and drops it. `--hairpin-snat-from CIDR`
(`bootstrap.hairpinSnatFrom`, usually the node CIDR, plus the Pod CIDR if your CNI does not
SNAT Pod egress) masquerades those connections as the VM. Only zlb-DNATed connections from
those sources are affected; external clients keep their address, and
`loadBalancerSourceRanges` still sees the original source. Traffic the VM itself sends to a
public IP is never forwarded; test from another host.

**Protocols.** TCP, UDP and SCTP can share a port number. Each needs conntrack/NAT support
on the VM and in your Kubernetes datapath; basic SCTP works, multihoming is not claimed.
Some CNIs limit SCTP NodePorts (for example
[Cilium](https://docs.cilium.io/en/stable/configuration/sctp/)).

## Configure pools and Services

### Pools

The controller re-reads its pool JSON (`pools` in the chart) every tick. Invalid JSON or
policy fails the tick and keeps the last accepted rules.

```json
{
  "default_pool": "public",
  "blocked_ports": [9808],
  "pools": [
    {"name": "public", "ips": ["198.51.100.10"],
     "reservations": [{"ip": "198.51.100.10", "ports": [80, 443], "service": "ingress/ingress"}]},
    {"name": "dedicated", "ips": ["198.51.100.11"], "services": ["apps/mail"],
     "reservations": [{"ip": "198.51.100.11", "service": "apps/mail"}]}
  ]
}
```

- `pools`: unique lowercase DNS-label names, explicit non-overlapping IPs (no CIDRs).
- `services`: optional exact `namespace/name` allowlist; omitted allows all, `[]` allows none.
- `reservations`: give an IP's literal `ports` (numbers or `[first, last]`) to one Service,
  or omit `ports` to give it the whole IP. A reservation holds while its owner is absent and
  publishes only ports the owner actually declares.
- `blocked_ports` (default `[9808]`): never published, literal or NodePort. A reservation
  with explicit `ports` cannot include one; a whole-IP reservation leaves them unpublished.

IP choice: the `zlb.zarrino.tech/ip` annotation if eligible (otherwise the Service fails);
else the Service's current IP; else an IP reserved to it; else the lowest pool IP not wholly
reserved to another Service (IPs are shared). It never spills into another pool.

Keep reserved ports outside the NodePort range. If two Services still resolve to the same
`(IP, protocol, port)`, the reservation owner keeps it, else the older Service; the other
fails alone with a Warning Event. Each tick the controller also asks the daemon
(`/v1/info`) what it accepts, so a Service the daemon would refuse fails alone too.

### Services

A plain `type: LoadBalancer` Service needs nothing else; Services without a class are adopted
like any default load balancer. `spec.loadBalancerClass: zlb.zarrino.tech/gateway` also
selects zlb; any other class is ignored.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: database
  namespace: apps
spec:
  type: LoadBalancer
  externalTrafficPolicy: Local
  selector:
    app: database
  ports:
    - name: tcp
      port: 5432
```

- **Published port.** Unreserved ports are published on their NodePort (here, say,
  `198.51.100.10:31543`); reserved ports on their Service port. After the daemon accepts the
  Service, the controller writes `zlb.zarrino.tech/published-ports: 5432:31543/TCP`
  (`port:published/PROTOCOL`, comma-separated) and removes it if the Service is rejected.
- **Annotations you set:** `zlb.zarrino.tech/pool` and `zlb.zarrino.tech/ip`.
  `spec.loadBalancerIP` is rejected.
- **Backends.** `Cluster` uses every Ready node with an InternalIP in the node CIDR that is
  not deleting or labelled `node.kubernetes.io/exclude-from-external-load-balancers`.
  `Local` uses only those with ready, non-terminating endpoints. zlb always targets
  `node:NodePort`, so keep `allocateLoadBalancerNodePorts` enabled.
- **Source ranges.** `loadBalancerSourceRanges` is enforced on the VM. Established
  connections survive a narrower list or a removed listener; zlb never flushes conntrack.

## Command-line flags

`--help` on each module lists everything; the chart sets them for you. Constraints worth knowing:

**Controller** (`python3 -m zlb.controller`):

| Flag | Meaning |
| --- | --- |
| `--lb-node URL` | daemon API, with port; HTTP also needs `--allow-private-http` |
| `--token-file` | bearer token, re-read for every request |
| `--ca-file`, `--client-cert`, `--client-key` | HTTPS trust and mutual TLS |
| `--pool-config FILE` or repeated `--ip` | pools, or one simple pool named `default` |
| `--node-cidr` | node InternalIPs (not Pod or Service CIDRs); must lie inside the daemon's |
| `--interval` (5) | seconds between ticks, ±10 % jitter, minimum 1 |
| `--resync` (600) | seconds between full relists of the watch caches, minimum 30 |
| `--ready-file` | timestamp written after each successful tick |

**Daemon** (`python3 -m zlb.natd`):

| Flag | Meaning |
| --- | --- |
| `--listen`, `--port` (9808), `--token-file` | API address and token |
| `--ip ADDR[/PREFIX]` (repeat) | allowed public IPs, assigned to the interface (default `/32`) |
| `--public-interface`, `--node-cidr` | where public IPs live; allowed backends |
| `--backend-port-range` (30000-32767) | allowed backend ports |
| `--reserved-port` (repeat) | ports never published; the API port always is |
| TLS, `--allow-from`, forwarding, isolation, hairpin | see the sections above |
| `--table` (zlb), `--state-dir` (/var/lib/zlb) | owned table and address journal |
| `--cleanup` | remove the owned table and addresses, then exit |

The daemon adds each configured address to the interface and records it in a journal; it
only ever removes addresses it recorded, never preexisting or primary ones, and rejects an
address already on another interface. Changing the interface or an owned prefix needs a
cleanup with the old settings first; never delete the journal.

## Operate

### Observe

zlb has no Prometheus metrics. Its signals:

```sh
k -n zlb-system logs deployment/zlb-controller --tail=50
k -n apps get events --field-selector involvedObject.name=database
ssh "$ZLB_LB_NODE_SSH" 'journalctl -u zlb-natd --no-pager -n 50
  nft list map ip zlb dispatch          # IP . protocol . port : jump l_<id>
  nft list counters table ip zlb        # c_<id>: new connections per listener
  curl -s --cacert your-ca.crt https://10.0.0.3:9808/healthz'  # liveness; add --cert/--key under mTLS
```

- **Controller:** `reconciled services= listeners= errors=` per tick; `service=ns/name
  error=...` for a rejected Service; `reconcile failed` when nothing was pushed or a write
  failed. Ready means a tick completed within the last 30 seconds.
- **Events:** reason `LoadBalancerSyncFailed`, at most every 5 minutes per unchanged error.
- **Daemon:** `applied revision= listeners=` per committed change (unchanged re-sends are
  silent), `rejected desired state` for a 400, `ROLLBACK FAILED` needs you, and
  `rejected API connection` for `--allow-from` or the connection limit.
- **`GET /v1/info`** (with the token): accepted wire versions, IPs, node CIDR, ports.

### Verify a Service

```sh
k -n apps get service database \
  -o jsonpath='{.status.loadBalancer.ingress[0].ip} {.metadata.annotations.zlb\.zarrino\.tech/published-ports}'
curl --noproxy '*' --connect-timeout 3 http://198.51.100.10:31543/   # from outside, matching protocol
```

Check the client address the application sees. To test removal, delete the Service, wait
for its finalizer to go, and open a new connection; existing ones may finish.

### Change pools or addresses

Route new addresses to the VM first. Add them to the daemon (`bootstrap.publicIPs` and
`helm upgrade`, or `--ip` in both unit lines, `daemon-reload` and a keep-marker restart),
then to the pools.
To remove one, take it out of the pools first, wait for an acknowledged tick, then remove it
from the daemon. A Helm change to pools restarts the controller; a mounted file is re-read
each tick (use a directory mount, not `subPath`).

### Restart and upgrade

systemd runs the unit's cleanup after every stop. It keeps forwarding after a crash (systemd
restarts the daemon) and when `/run/zlb/zlb.keep` exists, and removes it otherwise. The
marker covers one restart:

```sh
sudo touch /run/zlb/zlb.keep && sudo systemctl restart zlb-natd.service
```

A VM shutdown is a clean stop: after boot the daemon re-adds its addresses and an empty table,
and the next controller tick restores the listeners.

Upgrade the daemon before the controller: 0.5.0 controllers send wire version 3, which
older daemons reject with 400 (the controller then stays not Ready while old rules keep
serving). With SSH bootstrap `helm upgrade` does this in the right order. A preprovisioned
daemon: copy the new package to `/opt/zlb/zlb`, update the unit, `daemon-reload`,
keep-marker restart, then `helm upgrade`. From 0.4.0, whose unit ran as root with a
root-only token, first create the `zlb-natd` user and make `/etc/zlb/token`
`root:zlb-natd` 0640 (see the example unit), or the new daemon cannot read its token. Rolling back to chart 0.2.0 reinstalls the 0.4.0 daemon,
whose restart drops forwarding until the old controller's next push. Per-release changes
are in the [chart README](../charts/zlb/README.md#upgrade-notes).

### Tokens

Both sides re-read the token per request. Update the Secret and `/etc/zlb/token` together
(bootstrap rewrites it without a restart on its next run); expect 401s between the two.

### Capacity

The [wire limits](internals.md#wire-format-and-http-api) allow 4096 listeners in a
request of at most 2 MiB (2000 listeners over 500 nodes take ~220 KB). The real limit is
listeners × backend nodes. In manual measurements a changed snapshot took ~6 s to apply at
600 × 100 and ~29 s in total at 2000 × 100; each nft call has a 30 s timeout, so beyond
that size applies can fail. An unchanged re-send costs milliseconds at any size. Prefer
`Local` traffic policy or the exclusion label to keep backend lists short. With 20,000
EndpointSlices the controller cache measured ~22 MiB steady and ~19 MiB peak while
relisting (limit 128Mi). Size `nf_conntrack_max` on the VM for your peak concurrent
connections.

### Uninstall

Delete or convert your LoadBalancer Services while controller and daemon still work, and wait
for their `zlb.zarrino.tech/cleanup` finalizers to go. Then:

```sh
helm uninstall zlb -n zlb-system --kubeconfig "$ZLB_KUBECONFIG" --kube-context "$ZLB_CONTEXT" --wait
ssh "$ZLB_LB_NODE_SSH" 'systemctl disable --now zlb-natd.service'   # cleanup removes table and addresses
```

For a daemon outside systemd, stop it, then as root run `python3 -m zlb.natd --cleanup`
from `/opt/zlb` with its serving flags, including `--listen`. Cleanup does nothing while the
keep marker exists. If the VM is gone for good,
make sure nothing still forwards before removing finalizers by hand, and never remove other
finalizers.

## Troubleshoot

Start with the Service's Events, the controller log and the daemon log, then follow the
packet: upstream route/ARP → VM public NIC → zlb counter → node:NodePort → CNI and
NetworkPolicy → Pod → reply via the VM.

| Symptom | Check |
| --- | --- |
| Service stays pending | Its Warning Event. Class, pool and allowlist, requested IP, whole-IP reservations, blocked ports, `allocateLoadBalancerNodePorts`. The controller log for pool JSON errors (`invalid pool configuration fields` means an unknown or missing key), RBAC (`watch` is needed), token, TLS, or a node-CIDR mismatch with the daemon. |
| Controller not Ready | `reconcile failed` tracebacks: daemon unreachable, 401, TLS, 400 from an older daemon, a failed Service write. |
| Daemon won't start, or answers 400 | `journalctl -u zlb-natd`: IPv4 forwarding off, token shorter than 32 characters, interface missing, address on another interface, journal for another interface, a table without zlb's marker, another daemon holding the lock. |
| IP assigned, connection times out | You used the Service port instead of the published port; counter not increasing (upstream routing/ARP); NodePort unreachable from the VM (cluster path, NetworkPolicy, host firewall); a drop-policy forward chain elsewhere on the VM. |
| Listener has no backends | Node Ready, exclusion label, InternalIP outside the node CIDR, EndpointSlice readiness and port names for `Local`. New connections are dropped, never sent to a local socket. |
| Wrong client address, one-way traffic | Replies bypass the VM (return routing); `Cluster` policy SNAT; in-cluster clients need `--hairpin-snat-from`. |
| Listener collision Event | A reserved literal port equal to another Service's NodePort on the same IP; keep reservations outside the NodePort range. |
| Deletion hangs | The daemon must acknowledge removal before the finalizer is released; fix that path first. |
| Connection survives deletion or ACL change | Expected: existing conntrack flows finish. Test with a new connection. |
| UDP or SCTP fails, TCP works | Service protocol and NodePort, EndpointSlice port protocol, CNI support, kernel SCTP. UDP reuses a mapping per source port; vary it when testing balancing. |
| Bootstrap Job failed | Exit 255 is SSH (address, key, host key). Otherwise `journalctl -u zlb-natd` and `systemctl status zlb-natd` on the VM; apt or `systemd-analyze verify` failures leave nothing there, so rerun with `bootstrap.schedule` or inspect the VM by hand. |
| `nft` apply fails | The daemon log; a failed apply rolls back to the previous table and addresses. Never restore a whole firewall dump. |

A LoadBalancer IP does not override NetworkPolicy: allowing `namespaceSelector: {}` does not
admit internet clients or a node doing `Cluster` SNAT. zlb never creates policy exceptions.
