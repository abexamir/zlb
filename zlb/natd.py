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

from .addresses import Addresses
from .http_api import APIServer, make_handler, read_token, server_context
from .model import NODE_PORTS, VERSION, VERSIONS, digest, expand, port_range, validate_state

LOG = logging.getLogger(__name__)


def table_name(value):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value):
        raise ValueError("nftables table name must be 1-32 lowercase letters/digits/underscores")
    return value


def host_guards(public_interface, ips, isolate=False):
    """Forward and input rules protecting the load balancer node itself.

    With IPv4 forwarding on, the node would route anything a neighbour on the public segment
    sends it, including straight into the node CIDR. The forward guard drops new or invalid
    connections arriving on the public interface unless they were DNATed (zlb listeners and
    any DNAT the operator configured). The optional input isolation keeps local services such
    as sshd off the published IPs: only replies and ping reach the node there."""
    forward, local = [], []
    if public_interface:
        interface = f'iifname "{public_interface}"'
        forward = [f"{interface} ct state invalid,untracked counter drop",
                   f"{interface} ct state new ct status & dnat == 0 counter drop"]
    if isolate:
        addresses = "ip daddr { " + ", ".join(ips) + " }"
        local = [f"{addresses} ct state established,related accept",
                 f"{addresses} icmp type echo-request accept",
                 f"{addresses} counter drop"]
    return forward, local


KEY = "ipv4_addr . inet_proto . inet_service"


def marker(state):
    """Ownership and revision marker stored as the table comment."""
    return f"zlb:v{state['version']}:{digest(state)}"


def listener_id(listener):
    return digest([listener["ip"], listener["protocol"].lower(), listener["port"]])[:16]


def layout(state, ips, public_interface=None, isolate=False):
    """The owned table's expected shape: rules per chain, elements per map, counter names.

    Lookups go through verdict maps keyed by (IP, protocol, port), so the work per packet is
    constant instead of one rule per listener, and the table holds one chain per listener
    instead of one per backend."""
    forward_guard, input_guard = host_guards(public_interface, ips, isolate)
    chains = {"prerouting": 1, "forward": bool(ips) + len(forward_guard) + 1,
              "input": 1 + len(input_guard), "open": 2}
    counters = set()
    listeners = expand(state)
    for listener in listeners:
        if listener["backends"]:
            chains["l_" + listener_id(listener)] = 1
            counters.add("c_" + listener_id(listener))
            if listener["source_ranges"]:
                chains["a_" + listener_id(listener)] = 2
    maps = {"dispatch": sum(bool(x["backends"]) for x in listeners), "guard": len(listeners)}
    return chains, maps, counters


def render(state, node_cidr, table="zlb", allowed_ips=None, public_interface=None, isolate=False,
           counters=None):
    """A complete owned table, committed atomically across NAT and filtering hooks.

    `counters` maps counter names to (packets, bytes) carried over from the table being
    replaced, so per-listener statistics survive applies."""
    table_name(table)
    node_cidr = str(ipaddress.IPv4Network(node_cidr))
    ips = sorted(allowed_ips if allowed_ips is not None else {x["ip"] for x in state["listeners"]})
    revision = digest(state)
    dispatch, guard, objects = [], [], []
    for listener in expand(state):
        proto, lid = listener["protocol"].lower(), listener_id(listener)
        key = f"{listener['ip']} . {proto} . {listener['port']}"
        backends = listener["backends"]
        if not backends:
            # Nothing to forward to: never let the traffic reach a local socket either.
            guard.append(f"{key} : drop")
            continue
        sources = listener["source_ranges"]
        source = "ip saddr { " + ", ".join(sources) + " } " if sources else ""
        packets, octets = (counters or {}).get("c_" + lid, (0, 0))
        objects.append(f"counter c_{lid} {{ packets {int(packets)} bytes {int(octets)} }}")
        targets = ", ".join(f"{index} : {backend['ip']} . {backend['port']}" for index, backend in enumerate(backends))
        # Source ranges gate DNAT; numgen picks a backend round-robin per new connection.
        objects.append(f'chain l_{lid} {{\n    {source}counter name "c_{lid}" meta l4proto {proto} '
                       f"dnat ip to numgen inc mod {len(backends)} map {{ {targets} }}\n}}")
        dispatch.append(f"{key} : jump l_{lid}")
        if sources:
            objects.append(f"chain a_{lid} {{\n    ip daddr {node_cidr} ct status dnat {source}counter accept\n"
                           "    counter drop\n}")
            guard.append(f"{key} : jump a_{lid}")
        else:
            guard.append(f"{key} : jump open")
    def verdict_map(name, elements):
        body = f"map {name} {{\n    type {KEY} : verdict\n"
        if elements:
            body += "    elements = { " + ", ".join(elements) + " }\n"
        return body + "}\n"
    def chain(name, declaration, rules):
        head = f"    {declaration}\n" if declaration else ""
        return f"chain {name} {{\n{head}" + "".join(f"    {r};\n" for r in rules) + "}\n"
    forward_guard, input_guard = host_guards(public_interface, ips, isolate)
    original = "ct direction original ct original ip daddr . ct original protocol . ct original proto-dst vmap @guard"
    established = ([f"ct state established,related ct status dnat ct original ip daddr {{ {', '.join(ips)} }} "
                    f"ct reply ip saddr {node_cidr} counter accept"] if ips else [])
    return (f'table ip {table} {{\ncomment "{marker(state)}";\n'
            + verdict_map("dispatch", dispatch) + verdict_map("guard", guard)
            + "".join(item + "\n" for item in objects)
            # A DNATed listener flow must target the node CIDR; anything else for a listener
            # tuple (denied source, empty backends) is dropped before a local socket sees it.
            + chain("open", "", [f"ip daddr {node_cidr} ct status dnat counter accept", "counter drop"])
            + chain("prerouting", "type nat hook prerouting priority -110; policy accept;",
                    ["ip daddr . meta l4proto . th dport vmap @dispatch"])
            + chain("forward", "type filter hook forward priority -10; policy accept;",
                    established + forward_guard + [original])
            + chain("input", "type filter hook input priority -10; policy accept;", [original] + input_guard)
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
                 forbidden_ports=(), table="zlb", backend_ports=NODE_PORTS, guard_forwarding=True,
                 isolate_public=False):
        self.table = table_name(table)
        self.node_cidr = str(ipaddress.IPv4Network(node_cidr))
        self.forbidden_ports = forbidden_ports
        self.backend_ports = backend_ports
        self.addresses = Addresses(self.command, interface, allowed_ips, Path(state_dir) / f"{table}-addresses.json")
        self.allowed_ips = sorted(self.addresses.desired)
        self.guard_interface = interface if guard_forwarding else None
        self.isolate_public = isolate_public
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
        if not any(info.get("comment", "").startswith(f"zlb:v{version}:") for version in VERSIONS):
            raise RuntimeError("refusing to modify a table not owned by this zlb schema")
        return {"fingerprint": digest(stable(data)), "data": data, "comment": info["comment"]}

    def text(self):
        """The owned table as reloadable text, taken only when a commit may need rolling back."""
        return self.command("nft", "-s", "list", "table", "ip", self.table)

    def counters(self):
        data = json.loads(self.command("nft", "-j", "list", "counters", "table", "ip", self.table))
        return {x["counter"]["name"]: (x["counter"]["packets"], x["counter"]["bytes"])
                for x in data["nftables"] if "counter" in x}

    def batch(self, text, previous):
        return (f"delete table ip {self.table}\n" if previous else "") + text

    def install(self, text, previous, check=False):
        args = ["nft"] + (["--check"] if check else []) + ["--file", "-"]
        self.command(*args, input=self.batch(text, previous))

    def verify(self, state, actual):
        if actual is None or actual["comment"] != marker(state):
            raise RuntimeError("nftables did not acknowledge the expected revision")
        chains, rules, maps, counters = {}, {}, {}, set()
        for item in actual["data"]["nftables"]:
            if "chain" in item:
                chains[item["chain"]["name"]] = item["chain"]
            elif "rule" in item:
                rules[item["rule"]["chain"]] = rules.get(item["rule"]["chain"], 0) + 1
            elif "map" in item:
                maps[item["map"]["name"]] = len(item["map"].get("elem", []))
            elif "counter" in item:
                counters.add(item["counter"]["name"])
        expected_chains, expected_maps, expected_counters = layout(
            state, self.allowed_ips, self.guard_interface, self.isolate_public)
        if set(chains) != set(expected_chains):
            raise RuntimeError("nftables chain readback differs from desired state")
        if any(rules.get(name, 0) != count for name, count in expected_chains.items()):
            raise RuntimeError("nftables rule count differs from desired state")
        if maps != expected_maps or counters != expected_counters:
            raise RuntimeError("nftables map or counter readback differs from desired state")
        for name in ("prerouting", "forward", "input"):
            chain = chains[name]
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
            self.apply({"version": VERSION, "backend_sets": [], "listeners": []})

    def apply(self, state):
        state = validate_state(state, self.allowed_ips, self.node_cidr, self.forbidden_ports,
                               self.backend_ports)
        revision = digest(state)
        with self.lock:
            previous = self.snapshot()
            before_addresses = self.addresses.owned.copy()
            if previous and self.current_digest == revision and previous["fingerprint"] == self.last_fingerprint:
                # The verified table is unchanged and has not drifted: the controller re-sends
                # its full state every tick, so skip rendering, checking and committing.
                try:
                    self.addresses.add()
                    self.addresses.retire()
                except Exception:
                    self.restore_addresses(before_addresses)
                    raise
                return revision
            candidate = render(state, self.node_cidr, self.table, self.allowed_ips, self.guard_interface,
                               self.isolate_public, self.counters() if previous else None)
            # Validation happens before either interface or firewall changes.
            self.install(candidate, previous, check=True)
            rollback = self.text() if previous else None
            try:
                self.addresses.add()
                self.install(candidate, previous)
                actual = self.snapshot()
                self.verify(state, actual)
                self.addresses.retire()
            except Exception:
                self.current_digest, self.last_fingerprint = None, None
                self.restore_addresses(before_addresses)
                try:
                    now = self.snapshot()
                    if previous:
                        self.install(rollback, now)
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

    def restore_addresses(self, owned):
        try:
            self.addresses.restore(owned)
        except Exception:
            LOG.exception("ADDRESS ROLLBACK FAILED; operator attention required")

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
    parser.add_argument("--backend-port-range", type=port_range, default=NODE_PORTS,
                        help="Inclusive FIRST-LAST range backends may use; match the cluster's "
                             "NodePort range (default 30000-32767)")
    parser.add_argument("--tls-cert", help="Serve HTTPS with this certificate chain (PEM)")
    parser.add_argument("--tls-key", help="Private key for --tls-cert (PEM)")
    parser.add_argument("--tls-client-ca", help="Require client certificates signed by this CA (mTLS)")
    parser.add_argument("--allow-from", action="append", default=[], type=ipaddress.ip_network,
                        help="Accept API connections only from this CIDR (repeatable; default any)")
    parser.add_argument("--max-connections", type=int, default=16,
                        help="Concurrent API connections; extra connections are closed")
    parser.add_argument("--allow-foreign-forwarding", action="store_true",
                        help="Do not drop new forwarded connections from the public interface that "
                             "were not DNATed (only if this VM also routes other public traffic)")
    parser.add_argument("--isolate-public-ips", action="store_true",
                        help="Drop traffic to the public IPs that is not a listener, a reply or ping")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    if not args.cleanup and not args.token_file:
        parser.error("--token-file is required when serving")
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert and --tls-key must be used together")
    if args.tls_client_ca and not args.tls_cert:
        parser.error("--tls-client-ca requires --tls-cert and --tls-key")
    if args.max_connections < 1:
        parser.error("--max-connections must be at least 1")
    logging.basicConfig(level=logging.INFO)
    applier = Applier(args.ip, args.node_cidr, args.public_interface, args.state_dir,
                      args.reserved_port + [args.port], args.table, args.backend_port_range,
                      not args.allow_foreign_forwarding, args.isolate_public_ips)
    token = None if args.cleanup else read_token(args.token_file)
    tls = None
    if args.tls_cert and not args.cleanup:
        tls = server_context(args.tls_cert, args.tls_key, args.tls_client_ca)
    runtime = Path("/run/zlb")
    runtime.mkdir(mode=0o700, exist_ok=True)
    with (runtime / (applier.table + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.cleanup:
            applier.cleanup()
            return
        applier.initialize()
        APIServer((args.listen, args.port), make_handler(applier, token), tls,
                  args.allow_from, args.max_connections).serve_forever()


if __name__ == "__main__":
    main()
