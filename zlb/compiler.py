"""Pure, deterministic Service + Node + EndpointSlice -> load balancer node state compiler."""
import ipaddress
from dataclasses import dataclass

from .model import CLASS, PREFIX, PROTOCOLS, compact, port, validate_state
from .pools import PoolConfig, contains


@dataclass
class Plan:
    state: dict
    assignments: dict  # UID -> IP; only successful Services
    errors: dict       # UID -> visible warning


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
        addresses = sorted(a["address"] for a in node.get("status", {}).get("addresses", [])
                           if a["type"] == "InternalIP"
                           and ipaddress.ip_address(a["address"]) in network)
        if addresses:
            result[metadata["name"]] = addresses[0]
    return result


def compile_state(services, nodes, slices, pool, node_cidr):
    """Resolve public listeners from administrator policy and a complete API snapshot."""
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
    assignments, errors, listeners = {}, {}, []
    for svc in services:
        if not active(svc):
            continue
        meta, spec = svc["metadata"], svc["spec"]
        uid = meta["uid"]
        identity = f"{meta['namespace']}/{meta['name']}"
        annotations = meta.get("annotations", {})
        try:
            ports = spec.get("ports", [])
            if not ports:
                raise ValueError("Service has no ports")
            seen = set()
            for p in ports:
                if p.get("protocol", "TCP") not in PROTOCOLS:
                    raise ValueError("protocol must be TCP, UDP or SCTP")
                requested = port(p["port"])
                port(p.get("nodePort"))
                if (requested, p.get("protocol", "TCP")) in seen:
                    raise ValueError("duplicate protocol/port")
                seen.add((requested, p.get("protocol", "TCP")))
            if spec.get("loadBalancerIP"):
                raise ValueError("use annotation zlb.zarrino.tech/ip instead of loadBalancerIP")
            ranges = [str(ipaddress.IPv4Network(r, strict=False))
                      for r in spec.get("loadBalancerSourceRanges", [])]
            selected, choices = config.candidates(svc, identity)
            old_ips = [x.get("ip") for x in svc.get("status", {}).get("loadBalancer", {}).get("ingress", [])]
            ip = next((i for i in old_ips if i in choices), choices[0])
            resolved = []
            for p in ports:
                owner = selected.reserved_to(ip, p["port"])
                public_port = p["port"] if owner == identity else p["nodePort"]
                if contains(config.blocked_ports, public_port):
                    raise ValueError(f"port {public_port} is globally blocked")
                resolved.append(public_port)
            assignments[uid] = ip
            for p, public_port in zip(ports, resolved):
                protocol = p.get("protocol", "TCP")
                backends = addresses
                if spec.get("externalTrafficPolicy", "Cluster") == "Local":
                    key = (meta["namespace"], meta["name"], p.get("name", ""), protocol)
                    names = local_nodes.get(key, set())
                    backends = {n: addresses[n] for n in names}
                listeners.append(dict(ip=ip, port=public_port, protocol=protocol, source_ranges=ranges,
                                      backend_port=p["nodePort"], backend_set=tuple(sorted(backends.values()))))
        except (ValueError, TypeError, KeyError) as exc:
            errors[uid] = str(exc)
    return Plan(validate_state(compact(listeners), config.ips, node_cidr),
                assignments, errors)
