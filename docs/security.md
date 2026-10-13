# Security model

Studio runs patterns on a user's machine, against URLs the user supplies. Even so, a pattern must not be able to reach the machine's private network, leak the user's credentials, or corrupt the caller's protocol stream.

## Egress guard (on by default)

All pattern traffic goes through `databolt.patterns.net`, which is the `ctx.http` client. The guard enforces the same rules as Studio's `lib/studio/ssrf.ts`.

- **Blocked ranges:**
  - `0.0.0.0/8`, `10.0.0.0/8`, `100.64.0.0/10` (CGNAT and Tailscale), `127.0.0.0/8`, `169.254.0.0/16` (link-local and cloud metadata), `172.16.0.0/12`, `192.168.0.0/16`, `224.0.0.0/3`;
  - `::`, `::1`, `fc00::/7`, `fe80::/10`, `fec0::/10`, `ff00::/8`.

  IPv4-mapped (`::ffff:a.b.c.d`), IPv4-compatible (`::a.b.c.d`) and NAT64 (`64:ff9b::/96`) addresses are checked by the IPv4 address they embed. inet_aton forms such as `2130706433` and `0x7f.1` are parsed locally, so every OS gives the same verdict.
- **Blocked names:** `localhost`, `*.localhost`, `*.internal` (for example `metadata.google.internal`) and `*.local`.
- **Schemes:** only `http` and `https`. Unix sockets are refused.

The guard checks three times:

1. **Before the request**, for the first URL and every redirect hop: the scheme, the host rules, and *every* address the hostname resolves to. If any address is blocked, the host is blocked.
2. **At connect time**, inside the HTTP transport's network backend: the host is resolved again, every address is checked, and the socket connects to a vetted **IP literal**. A DNS answer that changes between steps 1 and 2 (DNS rebinding) therefore cannot reach a private address. TLS still verifies the certificate against the hostname.
3. **After connect:** the peer address the OS reports is checked once more.

**Redirects** are followed manually, up to 5 hops, and each hop is re-checked. When a hop changes origin (scheme, host or port), the request drops `Authorization`, `Proxy-Authorization`, `Cookie` and any auth header the pattern marks sensitive (for example a custom `X-API-Key`). A `303`, or a `301`/`302` after a POST, becomes a GET without a body.

**Proxies:** with `trust_env`, `HTTP(S)_PROXY` and `NO_PROXY` are honoured. Even then, the destination is still checked before the request, but the connect-time and post-connect checks apply to the proxy, which the operator chose.

**Size and time:**

- A single response larger than 25 MiB is an error.
- Total bytes and wall-clock time are budgeted per run; when a budget runs out, the run is truncated.
- `Retry-After` values over 120 seconds fail fast instead of stalling the run.

`allow_private: true` turns off the address and host checks, but not the scheme check. It exists for tests and for trusted local networks, and is opt-in for every run.

## Credential binding

Every credential goes through one chokepoint: the HTTP client checks every request, and
every redirect hop, before it is sent. A credential is a secret value or a value derived
from one (a Basic-auth header, an OAuth access token, a Basic client-auth header), and the
client finds it wherever it appears: in headers, the query string, the path or the body.
It is attached only where all three rules allow it:

1. **Origin.** For the REST and `http_get` patterns, the request must have the same origin
   as `client.base_url` (or the `url`), or its host must match `client.auth_hosts`. This
   applies to every auth type, including an `api_key` sent as a query param, to `${secrets.x}`
   templated into headers or params, and to the OAuth access token.
2. **No downgrade.** If `base_url` is https, credentials never go to a plain-http URL.
3. **`secret_hosts`.** When the caller binds secrets to hosts (runner protocol `secret_hosts`,
   CLI `--secret-host`), each secret, and everything derived from it, goes only to hosts on
   its list.
   - An exact entry matches that host only. A leading `*.` matches any subdomain, at any
     depth, but not the apex, and never an IP address.
   - A missing or empty list means the secret goes nowhere.

When a rule says no, the client removes the header or query param that carries the
credential and adds a warning. If the credential is in the path or the body, which cannot be
removed safely, it refuses the request (`secret_host_mismatch`).

Some cases fail before any request is made:

- **Bindings that cannot be met.** If `base_url`'s host is outside a secret's
  `secret_hosts`, or an OAuth `token_url` host is outside the client secret's, the run fails
  with exit 2 and `secret_host_mismatch`. The error names the secret and the host, never the
  value.
- **OAuth `token_url`.** It must be on `base_url`'s origin, or match `client.auth_hosts`,
  and it must be https when `base_url` is (otherwise `token_url_not_allowed`, exit 2).
- **Next-page links and absolute `resource.path` values** that leave `base_url`'s origin
  fail with `cross_origin_pagination`. A different port or scheme counts as leaving.
  `client.allow_cross_origin_pagination: true` follows them, but **without credentials**.

## Secrets

- **References, not values.** Params hold `${secrets.<name>}` references, never values. Secret fields in a schema carry `format: secret-ref` and reject literal values. The values arrive only in the protocol request's `secrets` object (stdin, never argv or the environment) or through the CLI's `--secret name=@env:VAR`.
- **Where values never appear.** Values are never written to state, manifests, output files or `source_label`.
- **Redaction.** Every value is registered with a redactor, together with its URL-encoded forms, its base64 form, the Basic-auth `user:pass` base64, and any OAuth access token obtained during the run. The redactor masks these as `***` in:
  - error messages;
  - warnings;
  - stderr progress lines;
  - anything a pattern `print()`s;
  - the protocol result itself, including preview records that echo a token back.
- **Bound, not broadcast.** Credentials go only where the binding above allows.

## Protocol integrity

In `protocol` mode, file descriptor 1 is duplicated to stderr for the whole run, and `sys.stdout` is replaced by a redacting wrapper around stderr. The single JSON result is written to a private handle on the real stdout. Nothing a pattern does can add a second line to stdout.

## Custom code: trust model

- **Standard patterns** (`origin: standard`) ship in this package and are reviewed here.
- **YAML custom patterns** are data. They can only configure an existing pattern (`extends`), so they inherit its guard and redaction and run no code.
- **Python custom patterns** and **package patterns** are arbitrary code running with the caller's privileges. They are **not sandboxed**. The only thing that protects them is that `ctx.http` is guarded, and a pattern that imports `socket` or `subprocess` itself bypasses that. The trust controls work like this:
  - A Python file is imported only when its bytes hash to the `sha256` the caller recorded at trust time. Any edit requires re-approval (`trust_mismatch`).
  - Studio shows the code read-only before "Trust & save locally", and keeps the trust record per sha256.
  - Install only packages you would `pip install` anyway.

## Reporting

See [SECURITY.md](../SECURITY.md) for private vulnerability reporting.
