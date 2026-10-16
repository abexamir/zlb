"""Strict wire format shared by the compiler and the privileged load balancer node daemon."""
import hashlib
import ipaddress
import json

CLASS = "zlb.zarrino.tech/gateway"
PREFIX = "zlb.zarrino.tech/"
FINALIZER = PREFIX + "cleanup"
PROTOCOLS = {"TCP", "UDP", "SCTP"}
VERSION = 3          # wire version the controller sends
VERSIONS = (2, 3)    # wire versions the daemon accepts
# Kubernetes' default --service-node-port-range. The controller only ever targets NodePorts.
NODE_PORTS = (30000, 32767)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    return value


def port_range(value):
    """Parse an inclusive FIRST-LAST port range such as 30000-32767."""
    try:
        first, last = (int(part) for part in str(value).split("-"))
    except ValueError:
        raise ValueError("port range must look like FIRST-LAST") from None
    if port(first) > port(last):
        raise ValueError("port range starts after its end")
    return first, last


def ipv4(value):
    address = ipaddress.ip_address(value)
    if address.version != 4:
        raise ValueError("only IPv4 is supported")
    return str(address)


def validate_state(state, allowed_ips, node_cidr, forbidden_ports=(), backend_ports=None):
    """Normalize the complete nftables input contract before touching the load balancer node.

    Version 2 lists every backend of every listener. Version 3, which the controller sends,
    lists each distinct set of backend node IPs once and gives listeners a `backend_port`
    and a `backend_set` index, so its size grows with listeners plus nodes rather than
    listeners times nodes. The daemon accepts both, so it can be upgraded first.

    `backend_ports`, when given, is the inclusive range every backend port must fall in. The
    daemon sets it to the cluster's NodePort range so that a stolen API token cannot publish
    arbitrary node services such as the kubelet, etcd or SSH."""
    if not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] not in VERSIONS:
        raise ValueError("unsupported state version")
    v3 = state["version"] == 3
    if set(state) != ({"version", "backend_sets", "listeners"} if v3 else {"version", "listeners"}):
        raise ValueError("expected version, " + ("backend_sets " if v3 else "") + "and listeners")
    if not isinstance(state["listeners"], list) or len(state["listeners"]) > 4096:
        raise ValueError("invalid listener list")
    network = ipaddress.ip_network(node_cidr)

    def node(value):
        target = ipv4(value)
        if ipaddress.ip_address(target) not in network:
            raise ValueError("backend outside node CIDR")
        return target

    def node_port(value):
        value = port(value)
        if backend_ports and not backend_ports[0] <= value <= backend_ports[1]:
            raise ValueError("backend port outside the allowed NodePort range")
        return value

    sets = []
    if v3:
        if not isinstance(state["backend_sets"], list) or len(state["backend_sets"]) > 4096:
            raise ValueError("invalid backend sets")
        for members in state["backend_sets"]:
            if not isinstance(members, list) or len(members) > 4096:
                raise ValueError("invalid backend set")
            sets.append(tuple(sorted({node(member) for member in members}, key=ipaddress.ip_address)))
    seen = set()
    normalized = []
    for item in state["listeners"]:
        fields = ({"ip", "port", "protocol", "source_ranges", "backend_port", "backend_set"} if v3
                  else {"ip", "port", "protocol", "backends", "source_ranges"})
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
        ranges = item["source_ranges"]
        if not isinstance(ranges, list) or len(ranges) > 1024:
            raise ValueError("invalid source ranges")
        ranges = sorted({str(ipaddress.IPv4Network(r, strict=False)) for r in ranges})
        listener = dict(ip=ip, port=public_port, protocol=item["protocol"], source_ranges=ranges)
        if v3:
            index = item["backend_set"]
            if type(index) is not int or not 0 <= index < len(sets):
                raise ValueError("backend_set must index backend_sets")
            listener.update(backend_port=node_port(item["backend_port"]), backend_set=sets[index])
        else:
            if not isinstance(item["backends"], list) or len(item["backends"]) > 4096:
                raise ValueError("invalid backends")
            backends = set()
            for backend in item["backends"]:
                if not isinstance(backend, dict) or set(backend) != {"ip", "port"}:
                    raise ValueError("invalid backend")
                backends.add((node(backend["ip"]), node_port(backend["port"])))
            listener["backends"] = [dict(ip=i, port=p) for i, p in sorted(backends)]
        normalized.append(listener)
    normalized.sort(key=lambda x: (x["ip"], x["protocol"], x["port"]))
    if not v3:
        return {"version": 2, "listeners": normalized}
    return compact(normalized)


def compact(listeners):
    """Canonical version-3 state from listeners carrying `backend_set` as a tuple of node IPs:
    only referenced sets, each once, in a deterministic order."""
    order = sorted({tuple(x["backend_set"]) for x in listeners},
                   key=lambda members: [int(ipaddress.ip_address(m)) for m in members])
    index = {members: position for position, members in enumerate(order)}
    return {"version": 3, "backend_sets": [list(members) for members in order],
            "listeners": [dict(x, backend_set=index[tuple(x["backend_set"])]) for x in listeners]}


def expand(state):
    """Listeners with an explicit backend list, whatever the wire version."""
    if state["version"] == 2:
        return state["listeners"]
    sets = state["backend_sets"]
    return [dict(ip=x["ip"], port=x["port"], protocol=x["protocol"], source_ranges=x["source_ranges"],
                 backends=[dict(ip=member, port=x["backend_port"]) for member in sets[x["backend_set"]]])
            for x in state["listeners"]]
