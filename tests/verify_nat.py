"""Real TCP/UDP/SCTP and nftables/address lifecycle tests in isolated Linux namespaces."""
import copy
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
from collections import Counter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from zlb.natd import Applier, listener_id, render, stable
from zlb.model import compact, digest

PUBLIC, CLIENT, CIDR = "198.51.100.10", "192.0.2.2", "10.20.0.0/24"
NAMES = ["zlb-gw", "zlb-client", "zlb-n1", "zlb-n2"]
SERVER = '''import socket,sys,threading
proto=sys.argv[1]; port=int(sys.argv[2])
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM if proto=="UDP" else socket.SOCK_STREAM,132 if proto=="SCTP" else 0)
s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(("0.0.0.0",port))
def serve(c,peer):
 try:
  f=c.makefile("rwb",buffering=0)
  for line in f: f.write((c.getsockname()[0]+" "+peer[0]+"\\n").encode())
 except (OSError,ValueError): pass
 finally: c.close()
if proto=="UDP":
 while True:
  data,peer=s.recvfrom(4096); s.sendto((socket.gethostbyname(socket.gethostname())+" "+peer[0]+"\\n").encode(),peer)
else:
 s.listen()
 while True:
  c,peer=s.accept(); threading.Thread(target=serve,args=(c,peer),daemon=True).start()
'''
# UDP server must report the destination node, not the container's hostname address.
SERVER = SERVER.replace('socket.gethostbyname(socket.gethostname())', 'sys.argv[3]')
CLIENT_CODE = '''import socket,sys
proto=sys.argv[1]
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM if proto=="UDP" else socket.SOCK_STREAM,132 if proto=="SCTP" else 0)
s.settimeout(1.5)
if len(sys.argv)>2 and sys.argv[2]: s.bind((sys.argv[2],0))
s.connect((sys.argv[3],int(sys.argv[4])) if len(sys.argv)>4 else ("198.51.100.10",80))
if proto=="UDP":
 s.send(b"ping\\n"); print(s.recv(4096).decode().strip(),flush=True)
else:
 f=s.makefile("rwb",buffering=0)
 for line in sys.stdin: f.write(line.encode()); print(f.readline().decode().strip(),flush=True)
'''


def command(*args, input=None):
    result = subprocess.run(args, input=input, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f"{args}: {result.stderr.strip()}")
    return result.stdout


def ns(name, *args, **kwargs):
    return command("ip", "netns", "exec", name, *args, **kwargs)


NODE_PORT = ("10.20.0.11", 30080)  # reachable only through the load balancer node's forwarding
LOCAL = (PUBLIC, 8080)              # a load balancer node-local service listening on all addresses


class NamespaceApplier(Applier):
    def command(self, *args, input=None):
        return ns("zlb-gw", *args, input=input)


def outside():
    result = json.loads(ns("zlb-gw", "nft", "-j", "list", "table", "ip", "sentinel"))
    return stable(result)


def request(proto="TCP", source=None, target=None):
    args = ["python3", "-c", CLIENT_CODE, proto, source or ""] + ([target[0], str(target[1])] if target else [])
    return ns("zlb-client", *args, input="ping\n").strip()


def denied(proto="TCP", source=None, target=None):
    try: result = request(proto, source, target)
    except RuntimeError: return
    raise AssertionError("blocked traffic reached an application: " + result)


def read_line(process):
    if not select.select([process.stdout], [], [], 5)[0]:
        raise AssertionError("test client timed out")
    result = process.stdout.readline().strip()
    assert result, "test client closed unexpectedly"
    return result


def main():
    existing = {line.split()[0] for line in command("ip", "netns", "list").splitlines()}
    if existing.intersection(NAMES):
        raise RuntimeError("test namespace names already exist; refusing to modify them")
    processes = []
    temporary = tempfile.TemporaryDirectory()
    try:
        for name in NAMES:
            command("ip", "netns", "add", name)
            ns(name, "ip", "link", "set", "lo", "up")
        command("ip", "link", "add", "zlb-wan", "type", "veth", "peer", "name", "zlb-client0")
        command("ip", "link", "set", "zlb-wan", "netns", "zlb-gw")
        command("ip", "link", "set", "zlb-client0", "netns", "zlb-client")
        ns("zlb-gw", "ip", "addr", "add", "192.0.2.1/24", "dev", "zlb-wan")
        ns("zlb-gw", "ip", "link", "set", "zlb-wan", "up")
        for address in (CLIENT, "192.0.2.3"):
            ns("zlb-client", "ip", "addr", "add", address + "/24", "dev", "zlb-client0")
        ns("zlb-client", "ip", "link", "set", "zlb-client0", "up")
        ns("zlb-client", "ip", "route", "add", "default", "via", "192.0.2.1")
        ns("zlb-gw", "ip", "link", "add", "zlb-br", "type", "bridge")
        ns("zlb-gw", "ip", "addr", "add", "10.20.0.1/20", "dev", "zlb-br")
        ns("zlb-gw", "ip", "link", "set", "zlb-br", "up")
        for index in (1, 2):
            name, bridge_end, node_end = f"zlb-n{index}", f"zlb-b{index}", f"zlb-e{index}"
            command("ip", "link", "add", bridge_end, "type", "veth", "peer", "name", node_end)
            command("ip", "link", "set", bridge_end, "netns", "zlb-gw")
            command("ip", "link", "set", node_end, "netns", name)
            ns("zlb-gw", "ip", "link", "set", bridge_end, "master", "zlb-br")
            ns("zlb-gw", "ip", "link", "set", bridge_end, "up")
            ns(name, "ip", "addr", "add", f"10.20.0.{10+index}/20", "dev", node_end)
            ns(name, "ip", "link", "set", node_end, "up")
            ns(name, "ip", "route", "add", "default", "via", "10.20.0.1")
        ns("zlb-gw", "sh", "-c", "echo 1 > /proc/sys/net/ipv4/ip_forward")
        ns("zlb-gw", "nft", "-f", "-", input='table ip sentinel { chain input { type filter hook input priority 0; policy accept; }; }\n')
        baseline = outside()
        processes.append(subprocess.Popen(["ip", "netns", "exec", "zlb-gw", "python3", "-c", SERVER,
            "TCP", "8080", "192.0.2.1"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
        for index, name in ((1, "zlb-n1"), (2, "zlb-n2"), (0, "zlb-gw")):
            for proto in ("TCP", "UDP", "SCTP"):
                process = subprocess.Popen(["ip", "netns", "exec", name, "python3", "-c", SERVER,
                    proto, "30080" if index else "80", f"10.20.0.{10+index}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                processes.append(process)
        time.sleep(0.4)
        for process in processes:
            if process.poll() is not None:
                raise AssertionError("protocol server unavailable: " + process.stderr.read().decode())
        a = NamespaceApplier([PUBLIC], CIDR, "zlb-wan", temporary.name)
        a.initialize()
        assert ("zlb-wan", PUBLIC + "/32") in a.addresses.inventory()
        print("PASS: configured public IP assigned automatically to the load balancer node interface", flush=True)
        state = {"version": 2, "listeners": [{"ip": PUBLIC, "port": 80, "protocol": proto,
            "source_ranges": [], "backends": [{"ip": "10.20.0.11", "port": 30080},
                                              {"ip": "10.20.0.12", "port": 30080}]}
            for proto in ("TCP", "UDP", "SCTP")]}
        a.apply(state)
        for proto in ("TCP", "UDP", "SCTP"):
            responses = [request(proto) for _ in range(8)]
            assert all(x.split()[1] == CLIENT for x in responses), (proto, responses)
            assert Counter(x.split()[0] for x in responses) == {"10.20.0.11": 4, "10.20.0.12": 4}, (proto, responses)
            print(f"PASS: {proto} same-port forwarding, real source IP, even round robin", flush=True)
        upgraded = compact([dict(x, backend_port=30080, backend_set=("10.20.0.11", "10.20.0.12"))
                            for x in [{k: v for k, v in item.items() if k != "backends"} for item in state["listeners"]]])
        assert a.snapshot()["comment"].startswith("zlb:v2:")
        a.apply(upgraded)
        assert a.snapshot()["comment"].startswith("zlb:v3:")
        for proto in ("TCP", "UDP"):
            assert request(proto).split()[1] == CLIENT
        a.apply(state)
        print("PASS: a v2-owned table is replaced by the equivalent compact v3 state and back", flush=True)
        tcp = "c_" + listener_id(state["listeners"][0])
        connections = a.counters()[tcp][0]
        assert connections >= 8, connections
        calls = []
        original_command = a.command
        def recording(*args, input=None):
            calls.append(args)
            return original_command(*args, input=input)
        with patch.object(a, "command", side_effect=recording):
            a.apply(copy.deepcopy(state))
        assert not any("--check" in c or "--file" in c for c in calls), calls
        print(f"PASS: unchanged state is acknowledged without nft --check or commit ({len(calls)} read-only calls)", flush=True)
        rebalanced = copy.deepcopy(state)
        rebalanced["listeners"][0]["backends"] = rebalanced["listeners"][0]["backends"][:1]
        a.apply(rebalanced)
        assert a.counters()[tcp][0] >= connections
        a.apply(state)
        print("PASS: per-listener connection counters survive table replacement", flush=True)
        accepted = a.snapshot()["fingerprint"]
        exposed = copy.deepcopy(state)
        exposed["listeners"][0]["backends"] = [{"ip": "10.20.0.11", "port": 22}]
        try: a.apply(exposed); raise AssertionError("non-NodePort backend accepted")
        except ValueError: pass
        assert a.snapshot()["fingerprint"] == accepted
        print("PASS: backends outside the NodePort range are rejected before any change", flush=True)
        router = NamespaceApplier([PUBLIC], CIDR, "zlb-wan", temporary.name, guard_forwarding=False)
        router.apply(state)
        for proto in ("TCP", "UDP"):
            assert request(proto, target=NODE_PORT).split()[0] == "10.20.0.11"
        a.apply(state)
        for proto in ("TCP", "UDP"):
            denied(proto, target=NODE_PORT)
            assert request(proto).split()[1] == CLIENT
        print("PASS: forward guard stops public clients routing into the node CIDR; listeners still work", flush=True)
        assert request(target=LOCAL).split()[1] == CLIENT
        isolated = NamespaceApplier([PUBLIC], CIDR, "zlb-wan", temporary.name, isolate_public=True)
        isolated.apply(state)
        denied(target=LOCAL)
        assert request().split()[1] == CLIENT
        a.apply(state)
        print("PASS: optional isolation hides load balancer node-local services on public IPs", flush=True)
        held = subprocess.Popen(["ip", "netns", "exec", "zlb-client", "python3", "-c", CLIENT_CODE, "TCP"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(held)
        held.stdin.write("before\n"); held.stdin.flush(); before = read_line(held)
        changed = copy.deepcopy(state)
        for item in changed["listeners"]:
            item["backends"] = [{"ip": "10.20.0.12", "port": 30080}]
            item["source_ranges"] = [CLIENT + "/32"]
        a.apply(changed)
        held.stdin.write("after\n"); held.stdin.flush()
        assert read_line(held) == before
        for proto in ("TCP", "UDP", "SCTP"):
            assert request(proto).split()[0] == "10.20.0.12"
            denied(proto, "192.0.2.3")
        print("PASS: atomic rule changes preserve established flow; all three source ACLs deny excluded client", flush=True)
        empty = copy.deepcopy(state)
        for item in empty["listeners"]: item["backends"] = []
        a.apply(empty)
        for proto in ("TCP", "UDP", "SCTP"): denied(proto)
        print("PASS: empty backends block all protocols including load balancer node-local sockets", flush=True)
        a.apply(changed)
        previous = a.snapshot()
        with patch("zlb.natd.render", return_value="invalid nft syntax;\n"):
            try: a.apply(state); raise AssertionError("invalid candidate accepted")
            except RuntimeError: pass
        assert a.snapshot()["fingerprint"] == previous["fingerprint"]
        print("PASS: nft --check rejects invalid batch without modifying forwarding", flush=True)
        with patch.object(a, "verify", side_effect=RuntimeError("injected post-commit failure")):
            try: a.apply(state); raise AssertionError("failed readback acknowledged")
            except RuntimeError: pass
        assert a.snapshot()["fingerprint"] == previous["fingerprint"]
        assert request().split()[0] == "10.20.0.12"
        print("PASS: failed post-commit readback rolls back the entire owned table", flush=True)
        restarted = NamespaceApplier([PUBLIC], CIDR, "zlb-wan", temporary.name)
        restarted.initialize()
        assert restarted.snapshot()["fingerprint"] == a.snapshot()["fingerprint"]
        assert restarted.addresses.owned == {PUBLIC + "/32"}
        print("PASS: restart preserves acknowledged forwarding and restores address ownership journal", flush=True)
        restarted.apply({"version": 2, "listeners": []})
        held.stdin.write("removed\n"); held.stdin.flush()
        assert read_line(held) == before
        assert outside() == baseline
        print("PASS: removing listeners preserves existing flow and leaves unrelated nftables untouched", flush=True)
        crash = subprocess.run(["ip", "netns", "exec", "zlb-gw", "python3", "-m", "zlb.natd", "--cleanup", "--listen", "127.0.0.1",
                                "--ip", PUBLIC, "--public-interface", "zlb-wan", "--node-cidr", CIDR, "--state-dir", temporary.name],
                               env=dict(os.environ, SERVICE_RESULT="signal"), capture_output=True, text=True, timeout=60)
        assert crash.returncode == 0 and "keeping zlb forwarding" in crash.stderr, crash.stderr
        assert restarted.snapshot() is not None
        assert ("zlb-wan", PUBLIC + "/32") in restarted.addresses.inventory()
        print("PASS: ExecStopPost cleanup after a crash keeps the table and addresses for the restart", flush=True)
        restarted.cleanup()
        assert restarted.snapshot() is None
        assert ("zlb-wan", PUBLIC + "/32") not in restarted.addresses.inventory()
        assert ("zlb-wan", "192.0.2.1/24") in restarted.addresses.inventory()
        assert outside() == baseline
        print("PASS: cleanup removes only owned table and assigned addresses, preserving primary address", flush=True)
    finally:
        for process in reversed(processes):
            process.terminate(); process.wait(timeout=5)
        for name in reversed(NAMES):
            subprocess.run(["ip", "netns", "del", name], capture_output=True)
        temporary.cleanup()


if __name__ == "__main__":
    main()
