# Developing zlb

zlb uses Python 3.11+ and the standard library. Clone
https://github.com/abexamir/zlb and open pull requests against `main`. Contributions
are licensed under Apache-2.0. Do not include credentials or deployment-specific state.

## Source layout

- `zlb/compiler.py`: pure Kubernetes snapshot to desired listener state.
- `zlb/pools.py`: administrator-owned pools, allowlists and reservations.
- `zlb/model.py`: versioned wire validation, normalization and digests.
- `zlb/controller.py`, `zlb/kube.py`: reconciliation and Kubernetes API access.
- `zlb/natd.py`: Linux native nftables renderer, apply, readback and cleanup.
- `zlb/addresses.py`: persistent interface-address ownership.
- `zlb/http_api.py`: authenticated request handling and token validation.
- `examples/`: portable installation templates using documentation addresses.
- `charts/zlb/`: portable Helm installation.

Keep the compiler pure, validate all data before rendering rules, and preserve unrelated
firewall ownership. A change to deletion, rollback, source ranges, port resolution or wire
normalization should include a focused regression test. Keep the load balancer node/backend allowlists
independent from Kubernetes pool policy.

Maintain a generic product boundary: no assumptions about a particular provider, CNI,
ingress controller, secret manager or registry in new examples. Pass network parameters
explicitly. Preserve stable API identifiers unless introducing a deliberate compatibility
change. Do not add cloud credentials or deployment-specific access policy to core code.

## Tests

Run the unit suite:

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q zlb tests
```

HTTP tests use a localhost HTTP server and needs permission to bind a local socket.

Run the real NAT integration suite on a Linux container runtime:

```sh
docker build -f tests/Dockerfile.nat -t zlb-nat-test:local .
docker run --rm --privileged --network none zlb-nat-test:local
```

Privilege is needed for disposable Linux network namespaces, veth links, a bridge and native
nftables rules. The test has no host mounts or external network, and Docker's bridge is not
part of the traffic path. It verifies real packet behavior, not just generated text.

The same container then runs `tests/verify_bootstrap.py`: the real SSH-bootstrap remote script
against a real daemon, with a minimal `systemctl` stand-in that honors the unit's ExecStart,
ExecStopPost and RuntimeDirectory. It covers first install, token rotation, upgrade, and
rollback of rejected candidates without losing forwarding.

The suite requires kernel SCTP support and fails explicitly if unavailable. It verifies TCP,
UDP and SCTP packet paths, source preservation, ACLs, empty-backend guards, connection survival,
interface assignment/cleanup, rollback and preservation of an unrelated table.

Do not use a production load balancer node or Kubernetes context as a substitute for these tests.
Live installation checks need explicit target-cluster configuration and authorization.
Never publish tokens, kubeconfigs, registry credentials or secret-bearing command output.

## Documentation and examples

README.md is the entry point; task-oriented guides live in `docs/`. Keep commands consistent
with CLI `--help` and parser behavior. Validate chart output with `helm lint charts/zlb -f examples/helm-values.yaml` and
`helm template zlb charts/zlb -f examples/helm-values.yaml`. Use documentation-only external IPs, example domains
and explicit registry placeholders. Keep installation-specific operational notes in that installation
repository rather than in this project.

Before a public release, choose a project license, review reference-deployment material, establish image/release provenance and a compatibility
matrix. Do not describe those tasks as already completed.

## Checks and publishing

Install optional documentation validation dependencies with
`python3 -m pip install -r requirements-dev.txt`, then run `python3 scripts/check_docs.py`.
The CI workflow tests Python 3.11, 3.13 and 3.14, Helm rendering, documentation examples,
Git history secret scanning and Linux NAT behavior. Actions are pinned by commit and
Dependabot proposes updates. Publication runs only after these checks pass.

See [release maintenance](docs/releases.md) for image tags, OCI chart versions and
release commands. Production credentials and cluster access are never required by CI.
