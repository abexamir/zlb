"""Strict wire format shared by the compiler and the privileged load balancer node daemon."""
import hashlib
import ipaddress
import json

CLASS = "zlb.zarrino.tech/gateway"
PREFIX = "zlb.zarrino.tech/"
FINALIZER = PREFIX + "cleanup"
PROTOCOLS = {"TCP", "UDP", "SCTP"}
VERSION = 2


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    return value


def ipv4(value):
    address = ipaddress.ip_address(value)
    if address.version != 4:
        raise ValueError("only IPv4 is supported")
    return str(address)


def validate_state(state, allowed_ips, node_cidr, forbidden_ports=()):
    """Normalize the complete nftables input contract before touching the load balancer node."""
    if not isinstance(state, dict) or set(state) != {"version", "listeners"}:
        raise ValueError("expected version and listeners")
    if type(state["version"]) is not int or state["version"] != VERSION:
        raise ValueError("unsupported state version")
    if not isinstance(state["listeners"], list) or len(state["listeners"]) > 4096:
        raise ValueError("invalid listener list")
    network = ipaddress.ip_network(node_cidr)
    seen = set()
    normalized = []
    for item in state["listeners"]:
        fields = {"ip", "port", "protocol", "backends", "source_ranges"}
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError("invalid listener fields")
        ip, public_port = ipv4(item["ip"]), port(item["port"])
        if ip not in allowed_ips or public_port in forbidden_ports:
            raise ValueError("listener outside load balancer node allowlist")
        if item["protocol"] not in PROTOCOLS:
            raise ValueError("protocol must be TCP, UDP or SCTP")
        key = (ip, item["protocol"], public_port)
        if key in seen:
            raise ValueError("duplicate listener")
        seen.add(key)
        if not isinstance(item["backends"], list) or len(item["backends"]) > 4096:
            raise ValueError("invalid backends")
        backends = set()
        for backend in item["backends"]:
            if not isinstance(backend, dict) or set(backend) != {"ip", "port"}:
                raise ValueError("invalid backend")
            target = ipv4(backend["ip"])
            if ipaddress.ip_address(target) not in network:
                raise ValueError("backend outside node CIDR")
            backends.add((target, port(backend["port"])))
        ranges = item["source_ranges"]
        if not isinstance(ranges, list) or len(ranges) > 1024:
            raise ValueError("invalid source ranges")
        ranges = sorted({str(ipaddress.IPv4Network(r, strict=False)) for r in ranges})
        normalized.append(dict(ip=ip, port=public_port, protocol=item["protocol"],
                               backends=[dict(ip=i, port=p) for i, p in sorted(backends)],
                               source_ranges=ranges))
    return {"version": VERSION, "listeners": sorted(normalized, key=lambda x: (x["ip"], x["protocol"], x["port"]))}
