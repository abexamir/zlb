# zlb

[![CI](https://github.com/abexamir/zlb/actions/workflows/ci.yml/badge.svg)](https://github.com/abexamir/zlb/actions/workflows/ci.yml)
[![Publish](https://github.com/abexamir/zlb/actions/workflows/publish.yml/badge.svg)](https://github.com/abexamir/zlb/actions/workflows/publish.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

zlb exposes Kubernetes `LoadBalancer` Services through a Linux load balancer node with reachable
external IP addresses. It is useful when cluster nodes cannot advertise those addresses
directly through a cloud load balancer, BGP, or layer-2 announcements.

An in-cluster controller selects addresses and node backends. A load balancer node daemon programs
DNAT rules, forwarding connections to Kubernetes NodePorts without terminating application connections.

```text
client → external IP:published port → Linux load balancer node → node:NodePort → workload
                     zlb-natd rules       Kubernetes service datapath
```

The load balancer node can be a VM or physical machine. No particular hosting provider, CNI, ingress
controller, secret manager, private registry, or public cloud is required. The essential
network requirement is that replies traverse the load balancer node.

## What it does

- Optionally provisions its external daemon over verified SSH on a dedicated VM.
- Installs the controller through a portable Helm chart.
- Manages named, static IPv4 pools with optional Service allowlists.
- Forwards TCP, UDP and SCTP, including shared port numbers across protocols.
- Assigns configured public IPs to the load balancer node interface and tracks address ownership.
- Shares external IPs across Services using their allocated NodePorts.
- Reserves individual literal ports, or a whole IP, for named Services.
- Selects backends according to `externalTrafficPolicy: Local` or `Cluster`.
- Applies `loadBalancerSourceRanges` and preserves source addresses on the load balancer node hop.
- Validates rule updates, verifies installed rules, and attempts rollback on failure.
- Publishes standard Service ingress status after the load balancer node acknowledges the state.
- Cleans up listeners before releasing Service finalizers.

An unreserved Service with `port: 5432` and `nodePort: 31543` is reached at
`external-IP:31543`. An ingress Service reserved for ports 80 and 443 is reached at
`external-IP:80` and `external-IP:443`. Reservations come from administrator configuration;
Services do not choose arbitrary literal public ports themselves.

## Compared with MetalLB and Cilium LB IPAM

The main distinction is **where public traffic enters the system**. zlb is intended for
networks where a separate machine can own the public IPs and reach Kubernetes NodePorts,
but cluster nodes cannot directly advertise those IPs to the upstream network.

- **MetalLB** allocates Service IPs and announces them from cluster nodes using ARP/NDP
  in layer-2 mode or BGP peering with routers. Once traffic reaches a node, the Kubernetes
  service datapath forwards it to workloads. It fits networks where the upstream can reach
  Service IPs through those nodes. See [MetalLB concepts](https://metallb.io/concepts/).
- **Cilium LB IPAM** allocates IPs to Services; it does not provide traffic forwarding or
  address advertisement by itself. Cilium's BGP Control Plane, L2 Announcements, or another
  suitable network integration supplies reachability. It fits Cilium deployments that can
  provide that path. See [Cilium LB IPAM](https://docs.cilium.io/en/stable/network/lb-ipam/).
- **zlb** allocates from static pools and programs an external Linux load balancer node:
  `client → public IP:published port → nftables DNAT → node:NodePort → workload`.
  It can assign the public addresses to that machine and optionally install its daemon
  over SSH. It does not require BGP or public-facing L2 access on Kubernetes nodes;
  upstream reachability to the load balancer node is still required.

**IP sharing alone is not unique to zlb.**
[MetalLB supports selective IP sharing](https://metallb.io/usage/#ip-address-sharing),
subject to sharing-key, port and traffic-policy/backend restrictions.
[Cilium supports sharing keys](https://docs.cilium.io/en/stable/network/lb-ipam/#sharing-keys)
for non-conflicting Service ports, with explicit permission for cross-namespace sharing.
zlb's different port model publishes ordinary Services on their allocated **NodePorts**,
while administrator reservations publish selected **literal Service ports** or dedicate
an entire IP. Thus two Services can both declare `port: 443` and share one public IP on
different high ports, while an ingress controller retains reserved public ports 80/443.

Prefer MetalLB or Cilium's integrated approach when your network supports node-based
Service-IP reachability and you want the normal `external-IP:Service-port` model. Choose
zlb when you specifically need an external forwarding machine and its shared-IP/port
policy. The trade-offs are an extra NAT hop, an external machine to operate, mandatory
reply routing through that machine, IPv4-only support, and no built-in load balancer node
HA. zlb still depends on the cluster's service datapath and can run with Cilium as its CNI.

Whichever implementation you choose, give each Service one LoadBalancer owner: scope
classes and allocation policies so zlb, MetalLB and Cilium IPAM do not compete for the same
unclassified Services. For zlb's configuration and limits, see the
[complete lifecycle guide](docs/lifecycle.md).

## Install

Public controller images are available at **`ghcr.io/abexamir/zlb`** for Linux amd64 and
arm64. The Helm chart is published as **`oci://ghcr.io/abexamir/charts/zlb`**.
Public pulls require no registry login.

Provide a reachable Linux load balancer node, routing/firewall policy, an existing API-token
Secret, and your own pool configuration. Then install the pinned chart:

```sh
helm upgrade --install zlb oci://ghcr.io/abexamir/charts/zlb \
  --version 0.2.0 --namespace zlb-system --create-namespace \
  --kubeconfig /absolute/path/to/target-kubeconfig \
  -f your-values.yaml --wait --timeout 15m
```

Start with [example values](examples/helm-values.yaml) and follow the
[installation guide](docs/installation.md) for Secret creation, optional SSH bootstrap,
network prerequisites, verification and uninstall. Helm installs the controller; it does
not create the external VM or configure upstream routing.

## Start here

1. Check the network requirements in the [installation guide](docs/installation.md).
2. Adapt [Helm values](examples/helm-values.yaml) and install the [chart](charts/zlb/README.md).
3. Configure [pools, reservations and Services](docs/configuration.md).
4. Verify traffic and learn the [operating procedures](docs/operations.md).

Further reading:

- [Complete installation and packet lifecycle](docs/lifecycle.md): commands, generated rules, diagrams and edge cases.
- [Architecture and failure behavior](docs/architecture.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Development and contribution guide](CONTRIBUTING.md)

## Current scope

zlb supports **IPv4 TCP, UDP and SCTP on Linux using native nftables**. It requires
Python 3.11+, nftables, iproute2, and kernel conntrack/NAT support for each enabled protocol.
There are no third-party Python runtime dependencies. Kubernetes must allocate NodePorts
and provide discovery/v1 EndpointSlices. Service status uses `ipMode: Proxy`.
Your Kubernetes service datapath must support each protocol and its NodePort port translation;
load balancer node protocol support cannot supply missing CNI capabilities. See [installation](docs/installation.md).

Source preservation through the load balancer node does not prevent a CNI from performing another SNAT
under `externalTrafficPolicy: Cluster`. Use `Local` and verify the complete packet path when
workloads require the original client address.

There is no IPv6, leader election, active backend probing, or general same-subnet hairpin
solution. The daemon API supports native TLS and mutual TLS. Run one controller for one load balancer node state. Daemon crashes,
upgrades and token rotations keep installed rules serving; only a deliberate
`systemctl stop` removes them.
See [architecture](docs/architecture.md) for failure behavior and address ownership.

The `zlb.zarrino.tech/*` annotations and class name are stable API identifiers;
they do not require access to any organization-specific infrastructure.

## Releases and development

Every push to `main` runs CI and publishes a `main` image, a `sha-<full-commit>` image, and
an OCI development chart such as `0.2.0-main.RUN.sha.COMMIT`. Development charts pin the
matching image digest. Version tags such as `v0.4.0` publish a versioned image and stable
chart; the source chart's version is independent of the application version.

Use stable chart versions and image digests for repeatable deployments. The image tag
`latest` tracks application releases, while `main` tracks development. See
[release maintenance](docs/releases.md) and [contributing](CONTRIBUTING.md).

zlb is early software with unit tests, isolated Linux NAT integration tests and deployment
verification. Its current limitations are listed above and in the lifecycle guide.
Licensed under [Apache-2.0](LICENSE). Report vulnerabilities through the
[security policy](SECURITY.md).
