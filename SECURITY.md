# Security policy

## Reporting a vulnerability

Please do **not** open a public issue. Use GitHub's private vulnerability reporting
(*Security → Report a vulnerability* on this repository). We aim to acknowledge reports
within 3 business days and to ship a fix or mitigation within 30 days.

## Scope

Of particular interest:

- bypasses of the egress guard (reaching loopback, private, link-local, CGNAT or cloud-metadata
  addresses with `allow_private` off), including via redirects or DNS rebinding;
- secret leakage into errors, logs, run results, state files or manifests;
- auth headers or credentials sent to an origin other than the configured API;
- the runner protocol writing anything other than one JSON result to stdout.

Custom Python patterns run with the privileges of the calling process. They are **not**
sandboxed beyond the egress-guarded `ctx.http` client, so running untrusted pattern code
is out of scope. See `docs/security.md`.

## Supported versions

Only the latest minor release receives security fixes while the package is 0.x.
