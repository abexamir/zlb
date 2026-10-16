# Security policy

Report suspected vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/abexamir/zlb/security/advisories/new).
Do not include credentials, private keys, kubeconfigs or sensitive network captures in public issues.

Use the latest application release and review the documented limitations before deploying.
zlb's optional SSH bootstrap has root-equivalent access to the chosen load balancer node.
Protect that key, restrict the daemon API to trusted sources, and use an external TLS proxy
when the API path is not private. The daemon has no native TLS.

See the [lifecycle guide](docs/lifecycle.md) for ownership, validation and rollback behavior.
