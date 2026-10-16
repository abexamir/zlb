"""Run the real SSH-bootstrap REMOTE script against a real zlb-natd, as root, in a private
network and mount namespace. A minimal systemd stand-in runs the installed unit's ExecStart
and ExecStopPost (with $SERVICE_RESULT) and removes RuntimeDirectory after each stop, so the
install, upgrade, rollback and token-rotation paths are exercised end to end. Needs root,
unshare, nftables and iproute2 (the NAT test container)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from zlb.bootstrap import REMOTE, SSHBootstrap  # noqa: E402
from zlb.controller import LoadBalancerNode  # noqa: E402

HARNESS = Path("/tmp/zlb-harness")
SYSTEMCTL = r'''#!/usr/bin/env python3
import os, pathlib, shlex, signal, subprocess, sys, time
SD = pathlib.Path("/tmp/zlb-harness/sd"); SD.mkdir(parents=True, exist_ok=True)
UNIT, LOADED, PID = pathlib.Path("/etc/systemd/system/zlb-natd.service"), SD / "loaded.unit", SD / "pid"
log = lambda message: open("/tmp/zlb-harness/calls", "a").write(message + "\n")
args = [a for a in sys.argv[1:] if not a.startswith("--quiet")]
verb = args[0] if args else ""
log("systemctl " + " ".join(sys.argv[1:]))
def setting(key):
    return next(l.split("=", 1)[1] for l in LOADED.read_text().splitlines() if l.startswith(key + "="))
def alive():
    status = pathlib.Path(f"/proc/{PID.read_text()}/status") if PID.exists() else None
    return bool(status and status.exists() and "State:\tZ" not in status.read_text())
def stop_post(result):
    log(f"ExecStopPost SERVICE_RESULT={result} keep={pathlib.Path('/run/zlb/zlb.keep').exists()}")
    subprocess.run(shlex.split(setting("ExecStopPost")), cwd=setting("WorkingDirectory"),
                   env=dict(os.environ, SERVICE_RESULT=result), stdout=open("/tmp/zlb-harness/daemon.log", "a"),
                   stderr=subprocess.STDOUT)
    for item in pathlib.Path("/run/zlb").iterdir():
        item.unlink()
def stop():
    if not PID.exists(): return
    if alive():
        os.kill(int(PID.read_text()), signal.SIGTERM)
        for _ in range(100):
            if not alive(): break
            time.sleep(0.1)
        stop_post("success")
    elif not (SD / "posted").exists():
        stop_post("exit-code")
    PID.unlink(); (SD / "posted").unlink(missing_ok=True)
def start():
    if alive(): return 0
    if not LOADED.exists(): return 5
    pathlib.Path("/run/zlb").mkdir(exist_ok=True)
    process = subprocess.Popen(shlex.split(setting("ExecStart")), cwd=setting("WorkingDirectory"),
                               stdout=open("/tmp/zlb-harness/daemon.log", "a"), stderr=subprocess.STDOUT,
                               start_new_session=True)
    PID.write_text(str(process.pid)); time.sleep(1.5)
    if process.poll() is not None and not (SD / "posted").exists():
        stop_post("exit-code"); (SD / "posted").touch()   # systemd runs ExecStopPost when it dies
    return 0
if verb == "daemon-reload":
    if UNIT.exists(): LOADED.write_text(UNIT.read_text())
    else: LOADED.unlink(missing_ok=True)
elif verb == "is-active":
    sys.exit(3 if pathlib.Path("/tmp/zlb-harness/fail-active").exists() or not alive() else 0)
elif verb == "stop": stop()
elif verb == "start": sys.exit(start())
elif verb == "restart": stop(); sys.exit(start())
elif verb == "enable" and "--now" in args: sys.exit(start())
'''
BROKEN = "        raise SystemExit('candidate fails after initialize')\n"
TOKEN = HARNESS / "token"


def run(*args):
    subprocess.run(args, check=True)


def isolate():
    """Private writable /etc and tmpfs over every path the bootstrap writes."""
    os.makedirs("/tmp/zlb-harness-etc", exist_ok=True)
    run("mount", "-t", "tmpfs", "tmpfs", "/tmp/zlb-harness-etc")
    for name in ("upper", "work"):
        os.makedirs(f"/tmp/zlb-harness-etc/{name}")
    run("mount", "-t", "overlay", "overlay", "-o",
        "lowerdir=/etc,upperdir=/tmp/zlb-harness-etc/upper,workdir=/tmp/zlb-harness-etc/work", "/etc")
    for path in ("/opt", "/etc/zlb", "/etc/systemd/system", "/var/lib/zlb-bootstrap", "/var/lib/zlb",
                 "/etc/sysctl.d", str(HARNESS), "/run/zlb"):
        os.makedirs(path, exist_ok=True)
        run("mount", "-t", "tmpfs", "tmpfs", path)
    bin = HARNESS / "bin"
    bin.mkdir()
    (bin / "systemctl").write_text(SYSTEMCTL)
    for name, body in (("systemctl", None), ("sysctl", "#!/bin/sh\nexit 0\n"), ("systemd-analyze", "#!/bin/sh\nexit 0\n")):
        if body:
            (bin / name).write_text(body)
        (bin / name).chmod(0o755)
    for command in ("ip link set lo up", "ip link add eth1 type veth peer name eth1p", "ip link set eth1 up"):
        run(*command.split())
    Path("/proc/sys/net/ipv4/ip_forward").write_text("1")
    return dict(os.environ, PATH=f"{bin}:{os.environ['PATH']}")


def payload(mutate=None):
    p = SSHBootstrap("127.0.0.1", "root", "/dev/null", "/dev/null", TOKEN, "127.0.0.1", 9808,
                     ["198.51.100.10/32"], "eth1", "10.20.0.0/24").payload()
    if mutate:
        mutate(p)
    return p


def broken(p):
    """A candidate that installs its table and address, then exits."""
    p["files"]["natd.py"] = p["files"]["natd.py"].replace("        APIServer((args.listen", BROKEN + "        APIServer((args.listen", 1)


def legacy(p):
    """Emulate a daemon from before restart safety: cleanup ignores $SERVICE_RESULT and the keep marker."""
    p["files"]["natd.py"] = p["files"]["natd.py"].replace("def cleanup_reason(", "def cleanup_reason(*_, **__):\n    return None\n\n\ndef unused_cleanup_reason(", 1)


def changed(p):
    p["files"]["natd.py"] += "\n# a new release\n"


def remote(p, env):
    result = subprocess.run([sys.executable, "-c", REMOTE], input=json.dumps(p), capture_output=True, text=True,
                            env=env, timeout=180)
    return result.returncode


def push(listeners):
    state = {"version": 3, "backend_sets": [["10.20.0.11"]], "listeners": [
        {"ip": "198.51.100.10", "port": 30000 + i, "protocol": "TCP", "source_ranges": [], "backend_port": 30000 + i,
         "backend_set": 0} for i in range(listeners)]}
    LoadBalancerNode("http://127.0.0.1:9808", TOKEN).apply(state)


def forwarding():
    listed = subprocess.run(["nft", "list", "map", "ip", "zlb", "dispatch"], capture_output=True, text=True)
    address = "198.51.100.10" in subprocess.run(["ip", "-br", "addr", "show", "eth1"], capture_output=True, text=True).stdout
    return (listed.stdout.count("jump") if listed.returncode == 0 else None), address


def calls(reset=True):
    path = HARNESS / "calls"
    lines = path.read_text().splitlines() if path.exists() else []
    if reset:
        path.unlink(missing_ok=True)
    return lines


def running():
    pid = HARNESS / "sd" / "pid"
    return pid.exists() and Path(f"/proc/{pid.read_text()}").exists()


def unhealthy_after_start():
    """The candidate starts and keeps running, but bootstrap's post-start check fails."""
    flag = HARNESS / "fail-active"
    def lifecycle():
        return [c for c in calls(False) if c.startswith(("systemctl restart", "systemctl stop", "systemctl start"))]
    def worker():
        while not lifecycle():
            time.sleep(0.02)
        flag.touch()
        while len(lifecycle()) < 2:
            time.sleep(0.02)
        flag.unlink(missing_ok=True)
    threading.Thread(target=worker, daemon=True).start()


def main():
    env = isolate()
    TOKEN.write_text("t" * 40)

    assert remote(payload(broken), env) != 0
    assert forwarding() == (None, False) and not Path("/opt/zlb/zlb").exists()
    assert not Path("/etc/systemd/system/zlb-natd.service").exists()
    print("PASS: a failed first install removes the table and address its candidate created", flush=True)

    assert remote(payload(), env) == 0 and running()
    push(2)
    calls()
    TOKEN.write_text("u" * 40)
    assert remote(payload(), env) == 0
    assert not [c for c in calls() if c.startswith(("systemctl restart", "systemctl stop", "systemctl start"))]
    push(2)
    print("PASS: token rotation is installed without restarting the daemon", flush=True)

    assert remote(payload(changed), env) == 0
    assert forwarding() == (2, True) and running()
    assert any("keep=True" in c for c in calls()), "the upgrade restart must keep forwarding"
    print("PASS: an upgrade restarts the daemon without interrupting forwarding", flush=True)

    for label, mutate, hook in (("exits after initialize", broken, None), ("runs but fails its health check", None, unhealthy_after_start)):
        if hook:
            hook()
        assert remote(payload(mutate), env) != 0
        assert forwarding() == (2, True), forwarding()
        assert running() and "# a new release" in Path("/opt/zlb/zlb/natd.py").read_text()
        calls()
        print(f"PASS: a rejected candidate that {label} is rolled back with forwarding intact", flush=True)

    assert remote(payload(legacy), env) == 0
    push(3)
    calls()
    unhealthy_after_start()
    assert remote(payload(changed), env) != 0
    assert forwarding() == (3, True), forwarding()
    assert running() and "unused_cleanup_reason" in Path("/opt/zlb/zlb/natd.py").read_text()
    print("PASS: rollback to a daemon whose cleanup ignores the keep marker keeps forwarding", flush=True)
    pid = HARNESS / "sd" / "pid"
    if pid.exists():
        try:
            os.kill(int(pid.read_text()), 9)
        except ProcessLookupError:
            pass


if __name__ == "__main__":
    if os.environ.get("ZLB_ISOLATED") != "1":
        os.execvpe("unshare", ["unshare", "-n", "-m", "--propagation", "private", sys.executable, __file__],
                   dict(os.environ, ZLB_ISOLATED="1"))
    main()
