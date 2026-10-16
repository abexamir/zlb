# Security policy

Report suspected vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/abexamir/zlb/security/advisories/new).
Never put credentials, private keys, kubeconfigs or sensitive packet captures in public issues.

Run the latest release. When deploying:

- The optional SSH bootstrap key is root-equivalent on the load balancer node. Only the
  bootstrap hook Job and its optional CronJob mount it; the controller never does.
  Protect it and the namespace's Secrets.
- Serve the daemon API over HTTPS, ideally with client certificates, and limit its sources
  with `--allow-from`. See [securing the daemon API](docs/guide.md#daemon-api).
- The daemon runs as the unprivileged `zlb-natd` user with `CAP_NET_ADMIN`, forwards only
  to NodePort-range backends, and by default refuses to route unsolicited public traffic
  into the node network.

[Internals](docs/internals.md) describes ownership, validation and rollback.
