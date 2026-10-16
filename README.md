<div align="center">

# zlb

**Many `LoadBalancer` Services on a few public IPs, through one hardened entry node.**

[![CI](https://github.com/abexamir/zlb/actions/workflows/ci.yml/badge.svg)](https://github.com/abexamir/zlb/actions/workflows/ci.yml)
[![Publish](https://github.com/abexamir/zlb/actions/workflows/publish.yml/badge.svg)](https://github.com/abexamir/zlb/actions/workflows/publish.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab)
![nftables](https://img.shields.io/badge/datapath-nftables-orange)

Public IPs are scarce and your nodes take no inbound internet traffic? Route the IPs to one
Linux machine and let zlb turn your Services into kernel DNAT rules there.

</div>

```mermaid
flowchart LR
    client([🌐 Client]) -->|198.51.100.10:31543| nft
    subgraph lb[Load balancer node]
        natd[zlb-natd] -.->|programs| nft[(nftables<br/>DNAT)]
    end
    subgraph k8s[Kubernetes cluster]
        node[node:NodePort] --> pod([Pod])
        ctl[zlb controller]
    end
    nft -->|client IP kept| node
    natd <==>|desired state ⇄ ack| ctl
```

## Designed for

- **Few public IPs, many Services.** Every Service shares the pool's IPs, published on its
  NodePort. Kubernetes already keeps NodePorts unique cluster-wide, so sharing never needs
  coordination.
- **Nodes without inbound internet access.** All external traffic enters through one machine
  that forwards only to NodePorts, enforces `loadBalancerSourceRanges` before the cluster,
  and refuses to route anything else into the node network: a DMZ for your Services.

Two PostgreSQL Services that both declare port 5432 share one IP without conflict:

```text
apps/orders-db   5432 → NodePort 31543   →  198.51.100.10:31543
apps/billing-db  5432 → NodePort 30876   →  198.51.100.10:30876
ingress/ingress  80, 443 (reserved)      →  198.51.100.10:80, 198.51.100.10:443
```

Literal ports such as 80/443, or a whole IP, are explicit administrator reservations, so the
scarce ones go where you decide.

## Compared with

| | MetalLB | Cilium LB IPAM | kube-vip | **zlb** |
| --- | --- | --- | --- | --- |
| Who answers for the public IP | a node (L2 leader) or all nodes (BGP) | a node (L2 Announcements) or BGP Control Plane | a node (ARP leader) or BGP | **one machine outside the cluster** |
| Network requirement | shared L2 segment or a BGP router | shared L2 segment or a BGP router | shared L2 segment or a BGP router | **IPs routed to that machine** |
| Nodes reachable from the internet | yes | yes | yes | **no** |
| Clients connect to | `IP:servicePort` | `IP:servicePort` | `IP:servicePort` | `IP:nodePort`, or reserved literal ports |
| Same port on one IP (two `:5432`) | no, needs a second IP | no, needs a second IP | no, needs a second IP | **yes** |
| IP sharing | opt-in annotation | opt-in sharing key | shared address | **default** |
| Datapath | kube-proxy or CNI on the node | eBPF on the node | kube-proxy or IPVS on the node | nftables DNAT on the entry node, then NodePort |
| Source ranges enforced | on the nodes | on the nodes | on the nodes | **on the entry machine** |
| Failover | yes (L2 leader election, BGP ECMP) | yes (L2 leases, BGP) | yes (leader election, BGP) | not yet ([planned](docs/internals.md#future-high-availability)) |
| Extra cost | speakers on nodes | requires Cilium | daemons on nodes | one machine, one NAT hop |

MetalLB, Cilium and kube-vip suit networks where nodes may own public addresses. zlb suits
networks where they must not, or cannot. [Cilium LB IPAM](https://docs.cilium.io/en/stable/network/lb-ipam/)
only allocates; Cilium's separate L2 Announcements or BGP Control Plane advertises. Let
exactly one implementation handle unclassified LoadBalancer Services.

## Features

| | |
| --- | --- |
| 🌐 **IP pools** | static IPv4 pools with optional per-pool Service allowlists; IPs are added to the machine for you |
| 🔌 **Ports** | Services share IPs on their NodePorts; reserve literal ports (80/443) or a whole IP |
| 📦 **Traffic** | TCP, UDP, SCTP · `Local` and `Cluster` policy · `loadBalancerSourceRanges` · client IPs preserved |
| 🛡️ **Safe changes** | `nft --check`, atomic commit, readback, rollback; forwarding survives crashes and upgrades |
| 🏷️ **Visible** | status, Events and a `published-ports` annotation on every Service |
| 🔐 **Secure** | TLS/mTLS API, sandboxed non-root daemon, controller confined by an admission policy |
| ⚙️ **Hands-off** | optional SSH bootstrap installs and upgrades the daemon from Helm |
| ⚡ **Fast** | one map lookup per packet; re-sending an unchanged state costs milliseconds |

## Quick start

**1 · Prepare a VM** that the public IPs route to and that can reach your nodes' NodePorts.

**2 · Create the API token** and a values file from
[examples/helm-values.yaml](examples/helm-values.yaml):

```sh
kubectl create namespace zlb-system
kubectl -n zlb-system create secret generic zlb-lb-node-token \
  --from-literal=token="$(openssl rand -hex 32)"
```

**3 · Install.** With `bootstrap.enabled`, Helm also installs the daemon on the VM over SSH.

```sh
helm upgrade --install zlb oci://ghcr.io/abexamir/charts/zlb --version 0.3.0 \
  -n zlb-system -f values.yaml --wait --timeout 15m
```

**4 · Expose** any Service with `type: LoadBalancer`. The [operator guide](docs/guide.md#install)
has the full walkthrough.

## Documentation

| | |
| --- | --- |
| 📘 [Operator guide](docs/guide.md) | install, security, networking, configuration, operations, troubleshooting |
| 🔧 [Internals](docs/internals.md) | reconcile loop, wire format, generated rules, packet path, failures |
| ⎈ [Chart](charts/zlb/README.md) · [Examples](examples/README.md) | chart and upgrade notes, sample inputs |
| 🤝 [Contributing](CONTRIBUTING.md) | development, tests, releases |

## Security

Report vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/abexamir/zlb/security/advisories/new),
never in public issues. Hardening (TLS/mTLS, `--allow-from`, the root-equivalent SSH
bootstrap key) is covered in [securing the load balancer node](docs/guide.md#secure-the-load-balancer-node).

**Scope:** IPv4 on Linux with nftables; one controller per load balancer node. No IPv6,
health probes or Prometheus metrics; in-cluster clients reach public IPs only with the
opt-in hairpin SNAT. Load balancer node HA is the next
[design target](docs/internals.md#future-high-availability). Apache-2.0.
