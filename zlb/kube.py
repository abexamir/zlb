"""Small in-cluster Kubernetes REST client. No shelling out or static bearer tokens."""
import json
import os
from pathlib import Path
import ssl
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen


class Kubernetes:
    def __init__(self):
        directory = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        self.token_path = directory / "token"
        self.context = ssl.create_default_context(cafile=str(directory / "ca.crt"))
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        if ":" in host:
            host = f"[{host}]"
        self.url = f"https://{host}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}"

    def open(self, method, path, body=None, content_type="application/json", timeout=15):
        headers = {"Authorization": "Bearer " + self.token_path.read_text().strip(),
                   "Content-Type": content_type}
        request = Request(self.url + path, data=None if body is None else json.dumps(body).encode(),
                          headers=headers, method=method)
        return urlopen(request, context=self.context, timeout=timeout)

    def request(self, method, path, body=None, content_type="application/json"):
        with self.open(method, path, body, content_type) as response:
            return json.load(response)

    def list_all(self, path, **params):
        return self.list_versioned(path, **params)[0]

    def list_versioned(self, path, **params):
        """All pages of a list and the resourceVersion the list is consistent at."""
        items, continuation, version = [], None, None
        while True:
            query = {"limit": 500, **params}
            if continuation:
                query["continue"] = continuation
            result = self.request("GET", path + "?" + urlencode(query))
            items.extend(result["items"])
            version = version or result.get("metadata", {}).get("resourceVersion")
            continuation = result.get("metadata", {}).get("continue")
            if not continuation:
                return items, version

    def watch(self, path, version, timeout=240):
        """Yield watch events from `version` until the server ends the stream."""
        query = urlencode({"watch": "1", "resourceVersion": version, "allowWatchBookmarks": "true",
                           "timeoutSeconds": int(timeout)})
        # The read timeout outlives the server's own so an idle but healthy stream is not cut
        # short, yet a silently dead connection is still noticed.
        with self.open("GET", path + "?" + query, timeout=timeout + 60) as response:
            for line in response:
                if line.strip():
                    yield json.loads(line)

    def patch(self, svc, field, value, status=False):
        meta = svc["metadata"]
        path = (f"/api/v1/namespaces/{quote(meta['namespace'], safe='')}/services/"
                f"{quote(meta['name'], safe='')}" + ("/status" if status else ""))
        operations = [{"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
                      {"op": "add", "path": field, "value": value}]
        return self.request("PATCH", path, operations, "application/json-patch+json")

    def warning(self, svc, message):
        from datetime import datetime, timezone
        meta = svc["metadata"]
        now = datetime.now(timezone.utc).isoformat()
        self.request("POST", f"/api/v1/namespaces/{meta['namespace']}/events", {
            "apiVersion": "v1", "kind": "Event",
            "metadata": {"generateName": "zlb-", "namespace": meta["namespace"]},
            "involvedObject": {"apiVersion": "v1", "kind": "Service", "name": meta["name"],
                               "namespace": meta["namespace"], "uid": meta["uid"]},
            "reason": "LoadBalancerSyncFailed", "message": message,
            "type": "Warning", "source": {"component": "zlb-controller"},
            "firstTimestamp": now, "lastTimestamp": now, "count": 1})
