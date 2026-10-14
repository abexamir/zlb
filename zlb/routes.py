"""Opt-in main-table routes with durable ownership and restoration of prior routes."""
import copy
import ipaddress
import json
from pathlib import Path
import re

from .addresses import atomic_json


def route(value):
    if not isinstance(value, dict) or not {"destination", "interface"} <= set(value) \
            or not set(value) <= {"destination", "interface", "gateway"}:
        raise ValueError("route requires destination, interface and optional gateway")
    destination = str(ipaddress.IPv4Network(value["destination"]))
    interface = value["interface"]
    if not isinstance(interface, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interface):
        raise ValueError("invalid route interface")
    gateway = str(ipaddress.IPv4Address(value["gateway"])) if value.get("gateway") is not None else None
    return {"destination": destination, "interface": interface, "gateway": gateway}


class Routes:
    def __init__(self, command, desired, journal):
        self.command, self.journal = command, Path(journal)
        items = [route(x) for x in desired]
        if len(items) > 64 or len({x["destination"] for x in items}) != len(items):
            raise ValueError("at most 64 routes with unique destinations are allowed")
        self.desired = {x["destination"]: x for x in items}
        self.owned = {}
        if self.journal.exists():
            data = json.loads(self.journal.read_text())
            if set(data) != {"version", "routes"} or data["version"] != 1 or not isinstance(data["routes"], dict):
                raise ValueError("invalid route ownership journal")
            self.owned = data["routes"]
            for key, entry in self.owned.items():
                if set(entry) != {"original", "installed"}:
                    raise ValueError("invalid owned route")
                for item in (entry["original"], entry["installed"]):
                    if item is not None:
                        self.arguments(item)
                        if item["destination"] != key:
                            raise ValueError("route journal destination mismatch")

    def save(self):
        atomic_json(self.journal, {"version": 1, "routes": self.owned})

    def inventory(self):
        rows = json.loads(self.command("ip", "-j", "-4", "route", "show", "table", "main"))
        result = {}
        for row in rows:
            destination = str(ipaddress.IPv4Network("0.0.0.0/0" if row.get("dst") == "default" else row["dst"]))
            if destination not in self.desired and destination not in self.owned:
                continue
            if destination in result or "nexthops" in row or row.get("type", "unicast") != "unicast":
                raise ValueError("managed destinations must have one simple unicast route")
            item = route({"destination": destination, "interface": row["dev"], "gateway": row.get("gateway")})
            item.update(metric=row.get("metric", 0), protocol=row.get("protocol", "boot"),
                        onlink="onlink" in row.get("flags", []), source=row.get("prefsrc"), scope=row.get("scope"))
            self.arguments(item)
            result[destination] = item
        return result

    @staticmethod
    def arguments(item):
        wanted = route({k: item[k] for k in ("destination", "interface", "gateway")})
        metric = item["metric"]
        if type(metric) is not int or not 0 <= metric <= 2**32 - 1:
            raise ValueError("invalid route metric")
        args = [wanted["destination"]]
        if wanted["gateway"]:
            args += ["via", wanted["gateway"]]
        args += ["dev", wanted["interface"], "metric", str(metric)]
        if item["onlink"]:
            args += ["onlink"]
        for field, flag in (("protocol", "proto"), ("scope", "scope")):
            if item.get(field) is not None:
                value = str(item[field])
                if not re.fullmatch(r"[A-Za-z0-9_]+", value):
                    raise ValueError("invalid route metadata")
                args += [flag, value]
        if item.get("source"):
            args += ["src", str(ipaddress.IPv4Address(item["source"]))]
        return args + ["table", "main"]

    def put(self, item):
        self.command("ip", "-4", "route", "replace", *self.arguments(item))

    def delete(self, item):
        self.command("ip", "-4", "route", "del", item["destination"], "dev", item["interface"],
                     "metric", str(item["metric"]), "table", "main")

    def snapshot(self):
        if not self.desired and not self.owned:
            return None
        return {"owned": copy.deepcopy(self.owned), "routes": self.inventory()}

    def restore(self, snapshot):
        if snapshot is None:
            return
        current = self.inventory()
        # Restore the original default before removing any management return routes.
        for key in sorted(set(current) | set(snapshot["routes"]), key=lambda x: (x != "0.0.0.0/0", x)):
            before, now = snapshot["routes"].get(key), current.get(key)
            if before == now:
                continue
            if before:
                self.put(before)
                if now and now["metric"] != before["metric"]:
                    self.delete(now)
            elif now:
                self.delete(now)
        self.owned = copy.deepcopy(snapshot["owned"])
        self.save()

    def reconcile(self):
        before = self.snapshot()
        if before is None:
            return None
        current = before["routes"]
        try:
            # Install management exceptions before switching the public default route.
            for key in sorted(self.desired, key=lambda x: (x == "0.0.0.0/0", x)):
                wanted, now = self.desired[key], current.get(key)
                if now and all(now[k] == wanted[k] for k in wanted):
                    continue
                if now and now["protocol"] == "kernel":
                    raise ValueError("refusing to replace a connected kernel route")
                installed = dict(wanted, metric=now["metric"] if now else self.owned.get(key, {}).get("installed", {}).get("metric", 0), protocol="static",
                                 onlink=bool(wanted["gateway"]), source=None, scope=None)
                self.owned.setdefault(key, {"original": now, "installed": installed})
                self.owned[key]["installed"] = installed
                self.save()  # claim before changing the kernel, including across a crash
                self.put(installed)
            for key in sorted(set(self.owned) - set(self.desired), key=lambda x: (x != "0.0.0.0/0", x)):
                entry = self.owned[key]
                now = current.get(key)
                if now and all(now[k] == entry["installed"][k] for k in ("destination", "interface", "gateway", "metric")):
                    if entry["original"]:
                        self.put(entry["original"])
                        if now["metric"] != entry["original"]["metric"]:
                            self.delete(now)
                    else:
                        self.delete(now)
                del self.owned[key]
                self.save()
            actual = self.inventory()
            if any(key not in actual or any(actual[key][k] != value for k, value in item.items())
                   for key, item in self.desired.items()):
                raise RuntimeError("route readback differs from desired state")
        except Exception:
            self.restore(before)
            raise
        return before

    def cleanup(self):
        desired, self.desired = self.desired, {}
        try:
            self.reconcile()
        finally:
            self.desired = desired
