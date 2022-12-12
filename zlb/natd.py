"""Authenticated nftables load balancer node with owned public interface addresses."""
import argparse
import fcntl
import ipaddress
import json
import logging
from pathlib import Path
import re
import subprocess
import threading
from http.server import ThreadingHTTPServer

from .addresses import Addresses
from .http_api import make_handler, read_token
from .model import VERSION, digest, validate_state

LOG = logging.getLogger(__name__)


def table_name(value):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value):
        raise ValueError("nftables table name must be 1-32 lowercase letters/digits/underscores")
    return value


def render(state, node_cidr, table="zlb", allowed_ips=None):
    """A complete owned table, committed atomically across NAT and filtering hooks."""
    table_name(table)
    node_cidr = str(ipaddress.IPv4Network(node_cidr))
    ips = sorted(allowed_ips if allowed_ips is not None else {x["ip"] for x in state["listeners"]})
    revision = digest(state)
    pre, filtering, destinations = [], [], []
    for listener in state["listeners"]:
        ip, public, proto = listener["ip"], listener["port"], listener["protocol"].lower()
        name = "l_" + digest([ip, proto, public])[:16]
        match = f"ip daddr {ip} meta l4proto {proto} {proto} dport {public}"
        original = (f"ct direction original ct original ip daddr {ip} "
                    f"ct original protocol {proto} ct original proto-dst {public}")
        backends = listener["backends"]
        if backends:
            sources = listener["source_ranges"]
            source = "ip saddr { " + ", ".join(sources) + " } " if sources else ""
            pre.append(f"{match} {source}jump {name}")
            filtering.append(f"{original} ip daddr {node_cidr} ct status dnat {source}counter accept")
            verdicts = []
            for index, backend in enumerate(backends):
                backend_name = f"{name}_{index}"
                verdicts.append(f"{index} : jump {backend_name}")
                destinations.append(f"chain {backend_name} {{ meta l4proto {proto} dnat to {backend['ip']}:{backend['port']}; }}")
            destinations.append(f"chain {name} {{ numgen inc mod {len(backends)} vmap {{ "
                                + ", ".join(verdicts) + " }; }")
        filtering.append(f"{original} counter drop")
    established = []
    for ip in ips:
        established.append(f"ct state established,related ct status dnat ct original ip daddr {ip} "
                           f"ct reply ip saddr {node_cidr} counter accept")
    def chain(name, declaration, rules):
        return f"chain {name} {{\n    {declaration}\n" + "".join(f"    {r};\n" for r in rules) + "}\n"
    return (f'table ip {table} {{\ncomment "zlb:v{VERSION}:{revision}";\n'
            + "\n".join(destinations) + "\n"
            + chain("prerouting", "type nat hook prerouting priority -110; policy accept;", pre)
            + chain("forward", "type filter hook forward priority -10; policy accept;", established + filtering)
            + chain("input", "type filter hook input priority -10; policy accept;", filtering)
            + "}\n")


def stable(value):
    if isinstance(value, dict):
        return {key: stable(item) for key, item in value.items()
                if key not in {"handle", "packets", "bytes", "metainfo"}}
    if isinstance(value, list):
        return [stable(item) for item in value if not isinstance(item, dict) or "metainfo" not in item]
    return value


class Applier:
    def __init__(self, allowed_ips, node_cidr, interface, state_dir="/var/lib/zlb",
                 forbidden_ports=(), table="zlb"):
        self.table = table_name(table)
        self.node_cidr = str(ipaddress.IPv4Network(node_cidr))
        self.forbidden_ports = forbidden_ports
        self.addresses = Addresses(self.command, interface, allowed_ips, Path(state_dir) / f"{table}-addresses.json")
        self.allowed_ips = sorted(self.addresses.desired)
        if not self.allowed_ips:
            raise ValueError("public IP allowlist cannot be empty")
        self.lock = threading.Lock()
        self.current_digest, self.last_fingerprint = None, None

    def command(self, *args, input=None):
        return subprocess.run(args, input=input, text=True, check=True,
                              capture_output=True, timeout=30).stdout

    def snapshot(self):
        inventory = json.loads(self.command("nft", "-j", "list", "tables"))["nftables"]
        if not any(x.get("table", {}).get("family") == "ip" and
                   x.get("table", {}).get("name") == self.table for x in inventory):
            return None
        data = json.loads(self.command("nft", "-j", "-s", "list", "table", "ip", self.table))
        info = next(x["table"] for x in data["nftables"] if "table" in x)
        if not info.get("comment", "").startswith(f"zlb:v{VERSION}:"):
            raise RuntimeError("refusing to modify a table not owned by this zlb schema")
        return {"text": self.command("nft", "-s", "list", "table", "ip", self.table),
                "fingerprint": digest(stable(data)), "data": data, "comment": info["comment"]}

    def batch(self, text, previous):
        return (f"delete table ip {self.table}\n" if previous else "") + text

    def install(self, text, previous, check=False):
        args = ["nft"] + (["--check"] if check else []) + ["--file", "-"]
        self.command(*args, input=self.batch(text, previous))

    def verify(self, state, actual):
        if actual is None or actual["comment"] != f"zlb:v{VERSION}:{digest(state)}":
            raise RuntimeError("nftables did not acknowledge the expected revision")
        chains = {x["chain"]["name"] for x in actual["data"]["nftables"] if "chain" in x}
        expected = {"input", "forward", "prerouting"}
        for item in state["listeners"]:
            if item["backends"]:
                name = "l_" + digest([item["ip"], item["protocol"].lower(), item["port"]])[:16]
                expected.add(name)
                expected.update(f"{name}_{i}" for i in range(len(item["backends"])))
        if chains != expected:
            raise RuntimeError("nftables chain readback differs from desired state")
        objects = actual["data"]["nftables"]
        nonempty = sum(bool(item["backends"]) for item in state["listeners"])
        expected_rules = (len(self.allowed_ips) + 2 * len(state["listeners"]) + 4 * nonempty
                          + sum(len(item["backends"]) for item in state["listeners"]))
        if sum("rule" in item for item in objects) != expected_rules:
            raise RuntimeError("nftables rule count differs from desired state")
        for item in objects:
            chain = item.get("chain", {})
            if chain.get("name") in {"prerouting", "forward", "input"}:
                name = chain["name"]
                if (chain.get("hook") != name or chain.get("prio") != (-110 if name == "prerouting" else -10)
                        or chain.get("type") != ("nat" if name == "prerouting" else "filter")):
                    raise RuntimeError("nftables hook readback differs from desired state")

    def initialize(self):
        self.command("nft", "--version")
        if Path("/proc/sys/net/ipv4/ip_forward").read_text().strip() != "1":
            raise RuntimeError("configure load balancer node IPv4 forwarding before starting zlb")
        self.addresses.inventory()
        # Keep an acknowledged table until a new snapshot arrives after a daemon restart.
        # New instances install an empty table and claim the configured interface addresses.
        if self.snapshot() is None:
            self.apply({"version": VERSION, "listeners": []})

    def apply(self, state):
        state = validate_state(state, self.allowed_ips, self.node_cidr, self.forbidden_ports)
        revision = digest(state)
        candidate = render(state, self.node_cidr, self.table, self.allowed_ips)
        with self.lock:
            previous = self.snapshot()
            before_addresses = self.addresses.owned.copy()
            # Validation happens before either interface or firewall changes.
            self.install(candidate, previous, check=True)
            try:
                self.addresses.add()
                if not (previous and self.current_digest == revision and
                        previous["fingerprint"] == self.last_fingerprint):
                    self.install(candidate, previous)
                actual = self.snapshot()
                self.verify(state, actual)
                self.addresses.retire()
            except Exception:
                self.current_digest, self.last_fingerprint = None, None
                try:
                    self.addresses.restore(before_addresses)
                except Exception:
                    LOG.exception("ADDRESS ROLLBACK FAILED; operator attention required")
                try:
                    now = self.snapshot()
                    if previous:
                        self.install(previous["text"], now)
                        restored = self.snapshot()
                        if restored["fingerprint"] != previous["fingerprint"]:
                            raise RuntimeError("nftables rollback readback mismatch")
                    elif now:
                        self.command("nft", "delete", "table", "ip", self.table)
                except Exception:
                    LOG.exception("LOAD BALANCER NODE ROLLBACK FAILED; operator attention required")
                raise
            self.current_digest, self.last_fingerprint = revision, actual["fingerprint"]
            LOG.info("applied revision=%s listeners=%d", revision, len(state["listeners"]))
            return revision

    def cleanup(self):
        if self.snapshot():
            self.command("nft", "delete", "table", "ip", self.table)
        self.addresses.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", required=True)
    parser.add_argument("--port", type=int, default=9808)
    parser.add_argument("--token-file")
    parser.add_argument("--ip", action="append", required=True, help="Allowed public IPv4 address, optionally /prefix")
    parser.add_argument("--public-interface", required=True)
    parser.add_argument("--node-cidr", required=True)
    parser.add_argument("--table", default="zlb")
    parser.add_argument("--state-dir", default="/var/lib/zlb")
    parser.add_argument("--reserved-port", type=int, action="append", default=[])
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    if not args.cleanup and not args.token_file:
        parser.error("--token-file is required when serving")
    logging.basicConfig(level=logging.INFO)
    applier = Applier(args.ip, args.node_cidr, args.public_interface, args.state_dir,
                      args.reserved_port + [args.port], args.table)
    token = None if args.cleanup else read_token(args.token_file)
    runtime = Path("/run/zlb")
    runtime.mkdir(mode=0o700, exist_ok=True)
    with (runtime / (applier.table + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.cleanup:
            applier.cleanup()
            return
        applier.initialize()
        ThreadingHTTPServer((args.listen, args.port), make_handler(applier, token)).serve_forever()


if __name__ == "__main__":
    main()
