"""End-to-end test in a kind cluster.

The controller is installed from the source chart with the image under test. A real zlb-natd
runs in a privileged container on the kind network (the load balancer node); a network
namespace inside it is the external client on the public side. The kind nodes get a return
route for the client network through the load balancer node, as the operator guide requires.

Needs docker, kind, kubectl and helm, a cluster created from tests/kind.yaml, the controller
image loaded into it, and the NAT test image (tests/Dockerfile.nat). Usage:
  e2e_kind.py CLUSTER CONTROLLER_IMAGE NAT_TEST_IMAGE
"""
import ipaddress
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLUSTER, IMAGE, NAT_IMAGE = sys.argv[1:4]
LB = "zlb-e2e-lb"
PUBLIC, RESERVED_IP = "198.51.100.10", "198.51.100.11"
CLIENT = "192.0.2.2"
TOKEN = "e2e-" + "t" * 60
SERVER = ("import http.server as h\n"
          "class H(h.BaseHTTPRequestHandler):\n"
          " def do_GET(s):\n"
          "  s.send_response(200); s.end_headers(); s.wfile.write(s.client_address[0].encode())\n"
          " def log_message(s, *a): pass\n"
          "h.ThreadingHTTPServer(('', 8080), H).serve_forever()\n")


def run(*args, input=None, check=True, timeout=120):
    result = subprocess.run(args, input=input, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip()


def kubectl(*args, **kwargs):
    return run("kubectl", "--context", f"kind-{CLUSTER}", *args, **kwargs)


def lb(*args, **kwargs):
    return run("docker", "exec", LB, *args, **kwargs)


def eventually(what, check, timeout=120):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            result = check()
            if result:
                return result
        except Exception as exc:  # the condition is not met yet
            last = exc
        time.sleep(2)
    raise AssertionError(f"timed out waiting for {what}" + (f": {last}" if last else ""))


def fetch(ip, port, timeout=5):
    """GET from the external client namespace; returns the address the Pod saw."""
    return lb("ip", "netns", "exec", "client", "python3", "-c",
              f"import urllib.request; print(urllib.request.urlopen('http://{ip}:{port}/', timeout={timeout}).read().decode())",
              timeout=timeout + 10)


def service(name):
    return json.loads(kubectl("-n", "e2e", "get", "service", name, "-o", "json"))


def published(name):
    svc = service(name)
    ingress = svc.get("status", {}).get("loadBalancer", {}).get("ingress", [])
    annotation = svc["metadata"].get("annotations", {}).get("zlb.zarrino.tech/published-ports")
    return (ingress[0]["ip"], annotation, svc) if ingress and annotation else None


def setup():
    subnet = next(c["Subnet"] for c in json.loads(run("docker", "network", "inspect", "kind"))[0]["IPAM"]["Config"]
                  if ipaddress.ip_network(c["Subnet"]).version == 4)
    nodes = run("kind", "get", "nodes", "--name", CLUSTER).split()
    run("docker", "rm", "-f", LB, check=False)
    run("docker", "run", "-d", "--name", LB, "--privileged", "--network", "kind", "--entrypoint", "sleep",
        NAT_IMAGE, "infinity")
    lb_ip = run("docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", LB)
    for command in ("sysctl -w net.ipv4.ip_forward=1", "ip netns add client",
                    "ip link add pub0 type veth peer name cli0", "ip link set cli0 netns client",
                    "ip addr add 192.0.2.1/24 dev pub0", "ip link set pub0 up",
                    f"ip netns exec client ip addr add {CLIENT}/24 dev cli0",
                    "ip netns exec client ip link set cli0 up", "ip netns exec client ip link set lo up",
                    "ip netns exec client ip route add default via 192.0.2.1", "mkdir -p /etc/zlb"):
        lb(*command.split())
    lb("sh", "-c", f"umask 077; printf %s {TOKEN} > /etc/zlb/token")
    for node in nodes:
        # Return routing: replies to external clients and calls to the public IPs go via the LB.
        run("docker", "exec", node, "ip", "route", "replace", "192.0.2.0/24", "via", lb_ip)
        run("docker", "exec", node, "ip", "route", "replace", "198.51.100.0/24", "via", lb_ip)
    start_daemon(lb_ip, subnet)
    return lb_ip, subnet, nodes


def start_daemon(lb_ip, subnet):
    run("docker", "exec", "-d", LB, "sh", "-c",
        f"echo $$ > /run/natd.pid; cd /src && exec python3 -m zlb.natd --listen {lb_ip} "
        f"--token-file /etc/zlb/token --public-interface pub0 --node-cidr {subnet} --ip {PUBLIC} "
        f"--ip {RESERVED_IP} --hairpin-snat-from {subnet} >> /var/log/natd.log 2>&1")
    def healthy():
        lb("python3", "-c", f"import urllib.request; urllib.request.urlopen('http://{lb_ip}:9808/healthz', timeout=2)")
        return True
    eventually("the daemon API", healthy, timeout=60)


def install(lb_ip, subnet):
    kubectl("create", "namespace", "zlb-system")
    kubectl("-n", "zlb-system", "create", "secret", "generic", "zlb-lb-node-token", f"--from-literal=token={TOKEN}")
    repository, tag = IMAGE.rsplit(":", 1)
    values = {"image": {"repository": repository, "tag": tag, "digest": "", "pullPolicy": "Never"},
              "lbNode": {"url": f"http://{lb_ip}:9808", "allowPrivateHTTP": True, "existingSecret": "zlb-lb-node-token"},
              "nodeCIDR": subnet, "interval": 2,
              "pools": {"default_pool": "public", "blocked_ports": [9808], "pools": [
                  {"name": "public", "ips": [PUBLIC, RESERVED_IP],
                   "reservations": [{"ip": RESERVED_IP, "ports": [80], "service": "e2e/reserved"}]}]}}
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as file:
        json.dump(values, file)
        file.flush()
        run("helm", "upgrade", "--install", "zlb", str(ROOT / "charts/zlb"), "--kube-context", f"kind-{CLUSTER}",
            "-n", "zlb-system", "-f", file.name, "--wait", "--timeout", "5m", timeout=400)


def workload():
    kubectl("create", "namespace", "e2e")
    manifest = {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "echo", "namespace": "e2e"},
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "echo"}},
                  "template": {"metadata": {"labels": {"app": "echo"}},
                               "spec": {"containers": [{"name": "echo", "image": IMAGE, "imagePullPolicy": "Never",
                                                        "command": ["python", "-c", SERVER],
                                                        "ports": [{"containerPort": 8080}],
                                                        "readinessProbe": {"tcpSocket": {"port": 8080}}}]}}}}] + [
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": "e2e"},
         "spec": {"type": "LoadBalancer", "externalTrafficPolicy": "Local", "selector": {"app": "echo"},
                  "ports": [{"name": "http", "port": 80, "targetPort": 8080}]}}
        for name in ("web", "reserved")]}
    kubectl("apply", "-f", "-", input=json.dumps(manifest))
    kubectl("-n", "e2e", "rollout", "status", "deployment/echo", "--timeout=180s", timeout=200)


def main():
    lb_ip, subnet, nodes = setup()
    install(lb_ip, subnet)
    workload()

    ip, ports, svc = eventually("web to be published", lambda: published("web"))
    node_port = svc["spec"]["ports"][0]["nodePort"]
    assert (ip, ports) == (PUBLIC, f"80:{node_port}/TCP"), (ip, ports)
    assert "zlb.zarrino.tech/cleanup" in svc["metadata"]["finalizers"]
    assert eventually("traffic to web", lambda: fetch(PUBLIC, node_port)) == CLIENT
    print(f"PASS: unreserved Service published on {PUBLIC}:{node_port}; the Pod sees the client address", flush=True)

    ip, ports, _ = eventually("reserved to be published", lambda: published("reserved"))
    assert (ip, ports) == (RESERVED_IP, "80:80/TCP"), (ip, ports)
    assert eventually("traffic to the reserved port", lambda: fetch(RESERVED_IP, 80)) == CLIENT
    print(f"PASS: reserved Service published on its literal port {RESERVED_IP}:80", flush=True)

    seen = eventually("hairpin traffic from a node", lambda: run(
        "docker", "exec", nodes[0], "curl", "-sf", "--max-time", "5", f"http://{PUBLIC}:{node_port}/"))
    assert seen == lb_ip, seen
    print("PASS: a node reaches the public IP through hairpin SNAT", flush=True)

    account = kubectl("-n", "zlb-system", "get", "deployment", "zlb-controller",
                      "-o", "jsonpath={.spec.template.spec.serviceAccountName}")
    denied = subprocess.run(["kubectl", "--context", f"kind-{CLUSTER}", "--as", f"system:serviceaccount:zlb-system:{account}",
                             "-n", "e2e", "annotate", "service", "web", "example.com/added=1"],
                            capture_output=True, text=True, timeout=60)
    assert denied.returncode and "zlb controller may not change" in denied.stderr, denied.stderr
    print("PASS: the admission policy denies the controller other Service changes", flush=True)

    lb("sh", "-c", "kill -9 $(cat /run/natd.pid)")
    assert fetch(PUBLIC, node_port) == CLIENT
    start_daemon(lb_ip, subnet)
    assert fetch(PUBLIC, node_port) == CLIENT
    print("PASS: forwarding continues while the daemon is killed and restarted", flush=True)

    kubectl("-n", "e2e", "delete", "service", "web", "--wait=true", "--timeout=120s", timeout=150)
    def closed():
        try:
            fetch(PUBLIC, node_port, timeout=3)
        except RuntimeError:
            return True
        return False
    eventually("the listener to be removed", closed, timeout=60)
    print("PASS: deleting the Service removes the listener and releases the finalizer", flush=True)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        for command in (["kubectl", "--context", f"kind-{CLUSTER}", "get", "all,events", "-A"],
                        ["kubectl", "--context", f"kind-{CLUSTER}", "-n", "zlb-system", "logs", "deployment/zlb-controller", "--tail=100"],
                        ["docker", "exec", LB, "cat", "/var/log/natd.log"],
                        ["docker", "exec", LB, "nft", "list", "ruleset"]):
            print("$", " ".join(command), flush=True)
            subprocess.run(command, check=False)
        raise
