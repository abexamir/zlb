# Security policy

Report suspected vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/abexamir/zlb/security/advisories/new).
Do not include credentials, private keys, kubeconfigs or sensitive network captures in public issues.

Use the latest application release and review the documented limitations before deploying.
zlb's optional SSH bootstrap has root-equivalent access to the chosen load balancer node.
Only the one-shot bootstrap Job mounts that key; the controller never receives it. Protect
the key and the namespace's Secrets, restrict the daemon API to trusted sources (`--allow-from`), and serve it over
HTTPS, ideally with client certificates, whenever the path is not fully private. See
[securing the daemon API](docs/installation.md#securing-the-daemon-api).

See the [lifecycle guide](docs/lifecycle.md) for ownership, validation and rollback behavior.
