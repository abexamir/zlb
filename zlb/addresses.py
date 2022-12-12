"""Journal ownership of interface addresses without taking over administrator-owned IPs."""
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".zlb-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Addresses:
    def __init__(self, command, interface, addresses, journal):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interface):
            raise ValueError("invalid public interface name")
        self.command, self.interface, self.journal = command, interface, Path(journal)
        self.desired = {}
        for value in addresses:
            address = ipaddress.IPv4Interface(value)
            ip = str(address.ip)
            if ip in self.desired:
                raise ValueError("duplicate configured public address")
            self.desired[ip] = str(address)
        self.owned = set()
        if self.journal.exists():
            data = json.loads(self.journal.read_text())
            if set(data) != {"version", "interface", "addresses"} or data["version"] != 1:
                raise ValueError("invalid address ownership journal")
            if data["interface"] != interface:
                raise ValueError("public interface changed; clean up the previous interface first")
            self.owned = {str(ipaddress.IPv4Interface(cidr)) for cidr in data["addresses"]}

    def save(self):
        atomic_json(self.journal, {"version": 1, "interface": self.interface,
                                   "addresses": sorted(self.owned)})

    def inventory(self):
        links = json.loads(self.command("ip", "-j", "address", "show"))
        if not any(link["ifname"] == self.interface for link in links):
            raise ValueError("configured public interface does not exist")
        return [(link["ifname"], f"{a['local']}/{a['prefixlen']}")
                for link in links for a in link.get("addr_info", []) if a["family"] == "inet"]

    def add(self):
        inventory = self.inventory()
        for ip, cidr in self.desired.items():
            present = [(name, value) for name, value in inventory
                       if str(ipaddress.IPv4Interface(value).ip) == ip]
            if any(name != self.interface for name, _ in present):
                raise ValueError(f"configured public IP {ip} exists on another interface")
            if present:
                if any(value in self.owned and value != cidr for _, value in present):
                    raise ValueError("owned address prefix changed; clean up that address first")
                continue
            self.command("ip", "address", "add", cidr, "dev", self.interface, "noprefixroute")
            self.owned.add(cidr)
            try:
                self.save()
            except Exception:
                self.command("ip", "address", "del", cidr, "dev", self.interface)
                self.owned.discard(cidr)
                raise

    def remove(self, cidr):
        if (self.interface, cidr) in self.inventory():
            self.command("ip", "address", "del", cidr, "dev", self.interface)
        self.owned.discard(cidr)
        self.save()

    def retire(self):
        for cidr in sorted(self.owned.copy()):
            if str(ipaddress.IPv4Interface(cidr).ip) not in self.desired:
                self.remove(cidr)

    def restore(self, owned):
        # Restore deleted owned addresses before removing additions from a failed apply.
        inventory = self.inventory()
        for cidr in sorted(owned - self.owned):
            ip = ipaddress.IPv4Interface(cidr).ip
            present = [(name, value) for name, value in inventory if ipaddress.IPv4Interface(value).ip == ip]
            if present and present != [(self.interface, cidr)]:
                raise RuntimeError("address ownership changed during rollback")
            if not present:
                self.command("ip", "address", "add", cidr, "dev", self.interface, "noprefixroute")
            self.owned.add(cidr)
            self.save()
        for cidr in sorted(self.owned - owned):
            self.remove(cidr)

    def cleanup(self):
        for cidr in sorted(self.owned.copy()):
            self.remove(cidr)
