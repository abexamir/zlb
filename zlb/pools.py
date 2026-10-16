"""Administrator-owned IP pools and listener reservations, independent of live Services."""
from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import re

from .model import PREFIX, ipv4, port


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise ValueError("invalid pool configuration fields")


def service_id(value):
    label = r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?"
    if not isinstance(value, str) or re.fullmatch(label + "/" + label, value) is None:
        raise ValueError("Service identity must be namespace/name")
    return value


def port_ranges(values):
    if not isinstance(values, list):
        raise ValueError("ports must be a list of numbers or [first, last] ranges")
    result = []
    for value in values:
        if type(value) is int:
            first = last = port(value)
        elif isinstance(value, list) and len(value) == 2:
            first, last = port(value[0]), port(value[1])
        else:
            raise ValueError("invalid port range")
        if first > last:
            raise ValueError("port range starts after its end")
        if any(first <= end and start <= last for start, end in result):
            raise ValueError("overlapping port ranges")
        result.append((first, last))
    return tuple(sorted(result))


def contains(ranges, number):
    return any(first <= number <= last for first, last in ranges)


@dataclass(frozen=True)
class Reservation:
    ip: str
    ports: tuple
    service: str
    whole_ip: bool  # the original JSON had no "ports" key -- the owner monopolizes this IP,
                     # not just the ports it happens to list (which default to the full range).


@dataclass(frozen=True)
class Pool:
    name: str
    ips: tuple
    services: tuple | None
    reservations: tuple

    def reserved_to(self, ip, number):
        """The identity that may publish `number` on `ip` verbatim, or None. A Service that
        isn't this owner never gets rejected for it -- it just publishes its own nodePort
        there instead (compiler.py), which Kubernetes already guarantees is collision-free."""
        return next((r.service for r in self.reservations if r.ip == ip and contains(r.ports, number)), None)

    def monopolized_against(self, ip, identity):
        """Only a *whole-IP* reservation still excludes a non-owner from `ip` entirely -- a
        specific-port reservation never needs to, since nothing else ever tries to publish
        that same literal number (unreserved Services always fall back to their nodePort)."""
        return any(r.ip == ip and r.whole_ip and r.service != identity for r in self.reservations)


@dataclass(frozen=True)
class PoolConfig:
    default_pool: str
    pools: dict
    blocked_ports: tuple

    @property
    def ips(self):
        return sorted((ip for pool in self.pools.values() for ip in pool.ips), key=ipaddress.IPv4Address)

    @classmethod
    def from_file(cls, path):
        return cls.parse(json.loads(Path(path).read_text()))

    @classmethod
    def simple(cls, ips):
        return cls.parse({"default_pool": "default", "pools": [{"name": "default", "ips": list(ips)}]})

    @classmethod
    def parse(cls, data):
        fields(data, {"default_pool", "pools"}, {"blocked_ports"})
        if not isinstance(data["pools"], list) or not data["pools"]:
            raise ValueError("at least one public IP pool is required")
        pools, all_ips = {}, set()
        blocked = port_ranges(data.get("blocked_ports", [9808]))
        for raw in data["pools"]:
            fields(raw, {"name", "ips"}, {"services", "reservations"})
            name = raw["name"]
            if not isinstance(name, str) or re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", name) is None:
                raise ValueError("pool name must be a lowercase DNS label")
            if name in pools:
                raise ValueError("duplicate pool name")
            if not isinstance(raw["ips"], list) or not raw["ips"]:
                raise ValueError("pool must contain an explicit non-empty IP list")
            # Numeric order, so 198.51.100.9 is offered before 198.51.100.10.
            ips = tuple(sorted((ipv4(ip) for ip in raw["ips"]), key=ipaddress.IPv4Address))
            if len(set(ips)) != len(ips) or all_ips.intersection(ips):
                raise ValueError("public IPs must not overlap within or across pools")
            all_ips.update(ips)
            services = raw.get("services")
            if "services" in raw:
                if not isinstance(services, list):
                    raise ValueError("services must be a namespace/name allowlist")
                services = tuple(sorted({service_id(value) for value in services}))
            reservations = []
            if not isinstance(raw.get("reservations", []), list):
                raise ValueError("reservations must be a list")
            for reservation in raw.get("reservations", []):
                fields(reservation, {"ip", "service"}, {"ports"})
                ip = ipv4(reservation["ip"])
                owner = service_id(reservation["service"])
                if ip not in ips:
                    raise ValueError("reserved IP is outside its pool")
                if services is not None and owner not in services:
                    raise ValueError("reservation owner is outside pool Service allowlist")
                whole_ip = "ports" not in reservation
                reserved = port_ranges(reservation.get("ports", [[1, 65535]]))
                if not reserved:
                    raise ValueError("reservation cannot have an empty ports list")
                if any(a <= d and c <= b for a, b in reserved for c, d in blocked):
                    # A whole-IP reservation can still exclude globally blocked management ports.
                    if "ports" in reservation:
                        raise ValueError("reservation overlaps globally blocked ports")
                for previous in reservations:
                    if ip == previous.ip and any(a <= d and c <= b
                                                for a, b in reserved for c, d in previous.ports):
                        raise ValueError("listener reservations overlap")
                reservations.append(Reservation(ip, reserved, owner, whole_ip))
            pools[name] = Pool(name, ips, services, tuple(reservations))
        if not isinstance(data["default_pool"], str) or data["default_pool"] not in pools:
            raise ValueError("default_pool must name a configured pool")
        return cls(data["default_pool"], pools, blocked)

    def candidates(self, service, identity):
        """Which pool this Service uses, and which of that pool's IPs it may land on -- ports
        play no part here (see Pool.reserved_to/monopolized_against): the only thing that can
        exclude a Service from an IP outright is someone else's whole-IP reservation."""
        meta = service["metadata"]
        annotations = meta.get("annotations", {})
        name = annotations.get(PREFIX + "pool", self.default_pool)
        if name not in self.pools:
            raise ValueError(f"unknown public IP pool: {name}")
        pool = self.pools[name]
        if pool.services is not None and identity not in pool.services:
            raise ValueError(f"Service is not allowed to use pool {name}")
        requested = annotations.get(PREFIX + "ip")
        if requested is not None:
            requested = ipv4(requested)
            if requested not in pool.ips:
                raise ValueError(f"requested IP is not in pool {name}")
            if pool.monopolized_against(requested, identity):
                raise ValueError(f"requested IP is reserved to another Service in pool {name}")
            return pool, [requested]
        available = [ip for ip in pool.ips if not pool.monopolized_against(ip, identity)]
        if not available:
            raise ValueError(f"every IP in pool {name} is reserved to another Service")
        # An IP this identity holds a reservation on is preferred over an arbitrary available
        # one -- otherwise a Service could land on an IP where its own reservation does
        # nothing, publishing its nodePort there while the IP it actually owns a literal port
        # on sits unused. compile_state() still prefers a valid "previous" IP over this order.
        owned = [ip for ip in available if any(r.ip == ip and r.service == identity for r in pool.reservations)]
        return pool, owned + [ip for ip in available if ip not in owned]
