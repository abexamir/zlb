# zlb

[![CI](https://github.com/abexamir/zlb/actions/workflows/ci.yml/badge.svg)](https://github.com/abexamir/zlb/actions/workflows/ci.yml)
[![Publish](https://github.com/abexamir/zlb/actions/workflows/publish.yml/badge.svg)](https://github.com/abexamir/zlb/actions/workflows/publish.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)

zlb gives Kubernetes `LoadBalancer` Services public IPs on a separate Linux machine, the
**load balancer node**, for clusters whose nodes cannot announce those IPs themselves (no
cloud load balancer, BGP or layer-2 access). A controller in the cluster decides what to
publish; a small daemon on the machine turns it into nftables DNAT rules to NodePorts.

```text
client → public IP:published port → load balancer node (nftables DNAT) → node:NodePort → Pod
```

## Features

- Static IPv4 pools, each with an optional allowlist of Services; IPs are assigned to the VM's
  interface and only addresses zlb added are ever removed.
- Many Services share one IP on their NodePorts; administrator reservations publish literal
  ports (such as 80/443 for ingress) or dedicate a whole IP.
- TCP, UDP and SCTP; `externalTrafficPolicy` `Local` and `Cluster`;
  `loadBalancerSourceRanges`; client source addresses preserved.
- Every change is checked with `nft --check`, committed atomically, read back, and rolled
  back on failure. Forwarding survives controller outages, daemon crashes and upgrades.
- Status and finalizer release follow the daemon's acknowledgement, so listeners are never orphaned.
  The published ports are recorded on each Service.
- TLS or mutual TLS for the daemon API, a non-root sandboxed daemon, optional SSH
  bootstrap of the VM from Helm, and an admission policy that confines the controller.

An unreserved Service with `port: 5432` and NodePort 31543 is reached at
`public-IP:31543` and annotated `zlb.zarrino.tech/published-ports: 5432:31543/TCP`.

## When to use it

[MetalLB](https://metallb.io/concepts/) and
[Cilium LB IPAM](https://docs.cilium.io/en/stable/network/lb-ipam/) give Services IPs that
the nodes themselves announce (ARP or BGP), keeping the usual `IP:Service-port` model;
prefer them when your network allows that. Choose zlb when only a separate machine can own
the public IPs. The costs: an extra NAT hop, a machine to run, replies that must route back
through it, IPv4 only, and no HA for that machine. Whatever you choose, let exactly one
implementation handle unclassified LoadBalancer Services.

## Install

Prepare the VM and its routing, create the API-token Secret and your values (start from
[examples/helm-values.yaml](examples/helm-values.yaml)), then:

```sh
helm upgrade --install zlb oci://ghcr.io/abexamir/charts/zlb --version 0.3.0 \
  --namespace zlb-system --create-namespace \
  --kubeconfig /absolute/path/to/target-kubeconfig --kube-context your-target-context \
  -f your-values.yaml --wait --timeout 15m
```

Images are `ghcr.io/abexamir/zlb` (amd64, arm64); public pulls need no login. Helm installs
the controller and, if enabled, sets up the daemon over SSH; it never creates the VM or its
routes.

## Documentation

- [Operator guide](docs/guide.md): requirements, install, security, networking,
  configuration, operations and troubleshooting.
- [Internals](docs/internals.md): reconciliation, wire format, generated rules, packet
  path, failure behaviour.
- [Chart](charts/zlb/README.md) and its upgrade notes, [examples](examples/README.md).
- [Contributing and releases](CONTRIBUTING.md), [security policy](SECURITY.md).

## Scope

IPv4 TCP, UDP and SCTP on Linux with nftables; Python 3.11+ with no third-party runtime
dependencies. One controller per load balancer node; no IPv6, load balancer node HA,
health probes or Prometheus metrics. In-cluster clients reach public IPs only with the
opt-in hairpin SNAT. zlb is early software, tested by unit tests and privileged Linux
integration suites. Licensed under [Apache-2.0](LICENSE).
