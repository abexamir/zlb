"""Pure, deterministic Service + Node + EndpointSlice -> load balancer node state compiler."""
import ipaddress
from dataclasses import dataclass, field

from .model import CLASS, PROTOCOLS, compact, port, validate_state
from .pools import PoolConfig, contains


@dataclass
class Plan:
    state: dict
    assignments: dict  # UID -> IP; only successful Services
    errors: dict       # UID -> visible warning
    published: dict = field(default_factory=dict)  # UID -> "port:published/PROTOCOL,..."


def owned(service):
    # Adopt default LoadBalancer Services while leaving explicitly foreign classes alone.
    lb_class = service.get("spec", {}).get("loadBalancerClass")
    return lb_class is None or lb_class == CLASS


def active(service):
    return (owned(service) and service.get("spec", {}).get("type") == "LoadBalancer"
            and not service["metadata"].get("deletionTimestamp"))


def node_addresses(nodes, node_cidr):
    network = ipaddress.ip_network(node_cidr)
    result = {}
    for node in nodes:
        metadata = node["metadata"]
        if metadata.get("deletionTimestamp"):
            continue
        if "node.kubernetes.io/exclude-from-external-load-balancers" in metadata.get("labels", {}):
            continue
        if not any(c["type"] == "Ready" and c["status"] == "True"
                   for c in node.get("status", {}).get("conditions", [])):
            continue
        addresses = sorted((a["address"] for a in node.get("status", {}).get("addresses", [])
                            if a["type"] == "InternalIP"
                            and ipaddress.ip_address(a["address"]) in network), key=ipaddress.IPv4Address)
        if addresses:
            result[metadata["name"]] = addresses[0]
    return result


def compile_state(services, nodes, slices, pool, node_cidr, constraints=None):
    """Resolve public listeners from administrator policy and a complete API snapshot.

    `constraints`, as reported by the load balancer node's /v1/info, are applied per Service
    so that a Service the daemon would refuse fails alone instead of failing the snapshot."""
    config = pool if isinstance(pool, PoolConfig) else PoolConfig.simple(pool)
    addresses = node_addresses(nodes, node_cidr)
    local_nodes = {}
    for es in slices:
        if es.get("addressType") != "IPv4":
            continue
        meta = es["metadata"]
        key = (meta["namespace"], meta.get("labels", {}).get("kubernetes.io/service-name"))
        for endpoint in es.get("endpoints") or []:
            conditions = endpoint.get("conditions", {})
            if conditions.get("ready") is False or conditions.get("terminating") is True:
                continue
            if endpoint.get("nodeName") in addresses:
                for entry in es.get("ports") or []:
                    if entry.get("port") is not None:
                        port_key = ((entry.get("name") or ""), entry.get("protocol", "TCP"))
                        local_nodes.setdefault((*key, *port_key), set()).add(endpoint["nodeName"])
    assignments, errors, listeners, claims = {}, {}, [], []
    node_ips = set(constraints["ips"]) if constraints else None
    forbidden = set(constraints.get("forbidden_ports", ())) if constraints else set()
    backend_ports = constraints.get("backend_ports") if constraints else None
    for svc in services:
        if not active(svc):
            continue
        meta, spec = svc["metadata"], svc["spec"]
        uid = meta["uid"]
        identity = f"{meta['namespace']}/{meta['name']}"
        try:
            ports = spec.get("ports", [])
            if not ports:
                raise ValueError("Service has no ports")
            seen = set()
            for p in ports:
                if p.get("protocol", "TCP") not in PROTOCOLS:
                    raise ValueError("protocol must be TCP, UDP or SCTP")
                requested = port(p["port"])
                if p.get("nodePort") is None:
                    raise ValueError(f"port {requested} has no nodePort; zlb forwards to NodePorts, so keep "
                                     "spec.allocateLoadBalancerNodePorts enabled")
                port(p["nodePort"])
                if (requested, p.get("protocol", "TCP")) in seen:
                    raise ValueError("duplicate protocol/port")
                seen.add((requested, p.get("protocol", "TCP")))
            if spec.get("loadBalancerIP"):
                raise ValueError("use annotation zlb.zarrino.tech/ip instead of loadBalancerIP")
            ranges = [str(ipaddress.IPv4Network(r, strict=False))
                      for r in spec.get("loadBalancerSourceRanges", [])]
            selected, choices = config.candidates(svc, identity)
            if node_ips is not None:
                choices = [i for i in choices if i in node_ips]
                if not choices:
                    raise ValueError("the load balancer node does not allow this Service's IP; "
                                     "add it to the daemon's --ip list")
            old_ips = [x.get("ip") for x in svc.get("status", {}).get("loadBalancer", {}).get("ingress", [])]
            ip = next((i for i in old_ips if i in choices), choices[0])
            resolved, literal = [], False
            for p in ports:
                owner = selected.reserved_to(ip, p["port"])
                public_port = p["port"] if owner == identity else p["nodePort"]
                literal = literal or owner == identity
                if contains(config.blocked_ports, public_port):
                    raise ValueError(f"port {public_port} is globally blocked")
                if public_port in forbidden:
                    raise ValueError(f"port {public_port} is reserved on the load balancer node")
                if backend_ports and not backend_ports[0] <= p["nodePort"] <= backend_ports[1]:
                    raise ValueError(f"nodePort {p['nodePort']} is outside the load balancer node's "
                                     f"backend range {backend_ports[0]}-{backend_ports[1]}")
                resolved.append(public_port)
            own = []
            for p, public_port in zip(ports, resolved, strict=True):
                protocol = p.get("protocol", "TCP")
                backends = addresses
                if spec.get("externalTrafficPolicy", "Cluster") == "Local":
                    key = (meta["namespace"], meta["name"], p.get("name", ""), protocol)
                    names = local_nodes.get(key, set())
                    backends = {n: addresses[n] for n in names}
                own.append(dict(ip=ip, port=public_port, protocol=protocol, source_ranges=ranges,
                                 backend_port=p["nodePort"], backend_set=tuple(sorted(backends.values()))))
            # Reservation owners claim first, then older Services, then by name.
            claims.append(((0 if literal else 1, meta.get("creationTimestamp", ""), identity), uid, ip, own,
                           [p["port"] for p in ports]))
        except (ValueError, TypeError, KeyError) as exc:
            errors[uid] = str(exc)
    # Two Services can resolve to the same public (IP, protocol, port), e.g. a literal
    # reservation equal to another Service's NodePort. The lower-ranked Service fails alone.
    taken = {}
    published = {}
    for (_, _, identity), uid, ip, own, service_ports in sorted(claims, key=lambda claim: claim[0]):
        keys = [(x["ip"], x["protocol"], x["port"]) for x in own]
        clash = next((key for key in keys if key in taken), None)
        if clash:
            errors[uid] = (f"public listener {clash[0]}:{clash[2]}/{clash[1]} is already used by "
                           f"{taken[clash]}")
            continue
        taken.update(dict.fromkeys(keys, identity))
        assignments[uid] = ip
        listeners.extend(own)
        # Unreserved ports are published on their NodePort, not the Service port, so record
        # the mapping on the Service in the style of kubectl's PORT(S) column.
        published[uid] = ",".join(f"{number}:{x['port']}/{x['protocol']}" for number, x in zip(service_ports, own, strict=True))
    return Plan(validate_state(compact(listeners), config.ips, node_cidr),
                assignments, errors, published)
