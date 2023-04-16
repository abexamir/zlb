"""Stop every other SSH bootstrap writer before an upgrade's bootstrap hook runs.

Controllers released before the bootstrap Job existed ran SSH bootstrap inside their
reconcile loop, and a drift-repair CronJob may still be running an older payload. Either
could reinstall an older daemon after the upgrade hook installed the new one. This one-shot
command runs as an earlier pre-upgrade hook with namespace-scoped API access and no SSH key.
It suspends the CronJob, deletes the Jobs it started, scales a legacy controller to zero and
waits until none of their pods remain."""
import argparse
import logging
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import quote

from .kube import Kubernetes

LOG = logging.getLogger(__name__)
MERGE = "application/merge-patch+json"


def legacy(deployment):
    """True when a controller Deployment still runs SSH bootstrap in its reconcile loop."""
    containers = deployment["spec"]["template"]["spec"].get("containers", [])
    return any(str(arg).startswith("--bootstrap-")
               for container in containers for arg in container.get("args", []) + container.get("command", []))


def optional(call):
    try:
        return call()
    except HTTPError as error:
        if error.code == 404:
            return None
        raise


def quiesce(kube, namespace, deployment, cronjob, instance=None, timeout=600, poll=2):
    ns = quote(namespace, safe="")
    batch = f"/apis/batch/v1/namespaces/{ns}"
    apps = f"/apis/apps/v1/namespaces/{ns}/deployments/{quote(deployment, safe='')}"
    # Bootstrap pods carry these template labels even after their Job object is gone, e.g.
    # when this hook is retried while a deleted Job's pod is still terminating.
    selectors = [f"app.kubernetes.io/component=bootstrap,app.kubernetes.io/instance={instance}"] if instance else []
    if optional(lambda: kube.request("PATCH", f"{batch}/cronjobs/{quote(cronjob, safe='')}",
                                     {"spec": {"suspend": True}}, MERGE)):
        LOG.info("suspended CronJob %s", cronjob)
    for job in kube.list_all(f"{batch}/jobs"):
        owners = job["metadata"].get("ownerReferences", [])
        if any(owner.get("kind") == "CronJob" and owner.get("name") == cronjob for owner in owners):
            name = job["metadata"]["name"]
            optional(lambda: kube.request("DELETE", f"{batch}/jobs/{quote(name, safe='')}",
                                          {"propagationPolicy": "Background"}))
            selectors.append(f"job-name={name}")
            LOG.info("deleted bootstrap Job %s", name)
    current = optional(lambda: kube.request("GET", apps))
    if current and legacy(current):
        kube.request("PATCH", apps + "/scale", {"spec": {"replicas": 0}}, MERGE)
        labels = current["spec"]["selector"]["matchLabels"]
        selectors.append(",".join(f"{key}={value}" for key, value in sorted(labels.items())))
        LOG.info("scaled legacy controller %s, which runs SSH bootstrap itself, to zero", deployment)
    deadline = time.monotonic() + timeout
    while True:
        remaining = sorted({pod["metadata"]["name"] for selector in selectors
                            for pod in kube.list_all(f"/api/v1/namespaces/{ns}/pods", labelSelector=selector)
                            if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")})
        if not remaining:
            LOG.info("no other bootstrap writers remain")
            return
        if time.monotonic() > deadline:
            raise RuntimeError("bootstrap writers still running: " + ", ".join(remaining))
        time.sleep(poll)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", help="Defaults to the pod's own namespace")
    parser.add_argument("--deployment", required=True, help="Controller Deployment name")
    parser.add_argument("--cronjob", required=True, help="Drift-repair bootstrap CronJob name")
    parser.add_argument("--instance", help="Helm release name labelling bootstrap pods")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    namespace = args.namespace or Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace").read_text().strip()
    try:
        quiesce(Kubernetes(), namespace, args.deployment, args.cronjob, args.instance, args.timeout)
    except Exception:
        LOG.exception("could not stop other bootstrap writers")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
