# Custom patterns in YAML

A YAML custom pattern **extends** an installed pattern, usually `databolt.ingestion.api.rest`. It exposes a few friendly params and hides the rest of the config. It runs no code of its own, so it inherits the base pattern's egress guard, retries and redaction. This is the safe default for an API the REST pattern can already describe.

```yaml
kind: databolt.pattern/v1
id: acme.orders                 # dotted, lowercase: <namespace>.<name>
version: 1.0.0                  # semver of *this* pattern
title: Acme orders
description: Orders from the Acme API, filtered by status.
icon: package
extends: databolt.ingestion.api.rest
extends_version: "^1"           # optional range for the base pattern

params:                         # what the user fills in
  status:
    type: string
    enum: [open, closed]
    default: open
  since:
    type: string
    format: date
    required: true
  token:
    type: string
    format: secret-ref          # a ${secrets.x} reference, never a value
    default: "${secrets.acme_token}"

secrets:
  - name: acme_token
    description: Acme API token

config:                         # the base pattern's params, with templates
  client:
    base_url: https://api.acme.com/v1/
    auth: {type: bearer, token: "${params.token}"}
    paginator: {type: cursor, cursor_path: meta.next, cursor_param: after}
  resource:
    path: orders
    params: {status: "${params.status}", since: "${params.since}"}
    data_selector: data
    incremental: {cursor_path: updated_at, param: updated_since, transform: iso8601, order: asc}

examples:
  - title: Closed orders this year
    params: {status: closed, since: "2026-01-01"}
```

## Params

Each entry is a small subset of JSON Schema:

| Key | Meaning |
|---|---|
| `type` | `string`, `integer`, `number`, `boolean`, `array` or `object` |
| `title`, `description` | Shown in the form. |
| `default`, `required` | A required param with no default must be supplied. |
| `enum` | Restricts the value to a list. Rendered as a select. |
| `format` | `secret-ref`, or a display hint such as `date`, `date-time` or `uri`. |
| `minimum`, `maximum` | Bounds for numbers. |

The manifest entry's `params_schema` is generated from these entries, exactly as for a standard pattern.

## Templates and when they render

- `${params.x}` is filled in when the pattern is bound to params. A string that is exactly one placeholder keeps the value's type: `max_pages: ${params.pages}` becomes an integer.
- `${secrets.x}` and `${state.cursor}` stay as templates and render at run time, inside the base pattern.
- `$${` produces a literal `${`.

## Validation happens at load time

`databolt-patterns validate acme.yaml` checks the file. The protocol does the same when it receives `{"pattern": {"yaml": "..."}}`. A file is rejected if any of these apply:

- `kind` is not `databolt.pattern/v1`;
- a key is unknown;
- `id` or `version` is invalid;
- `extends` is missing, or doesn't match `extends_version`;
- `config` references an undeclared `${params.x}`;
- `config` doesn't fit the base pattern's schema. This is checked by binding placeholder values: defaults, the first enum value, or a typed dummy.

## Running

```bash
databolt-patterns run acme.yaml --param status=closed --param since=2026-01-01 \
    --secret acme_token=@env:ACME_TOKEN --out out/
databolt-patterns list --custom ./patterns/      # every *.yaml in the directory
```

DataBolt Studio stores these files in `.data/studio/patterns/custom/` and sends the YAML text in the protocol request. Publishing to the community catalog rejects files that contain literal secrets.
