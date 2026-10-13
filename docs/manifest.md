# Manifest v1

The manifest describes every available pattern. DataBolt Studio builds its palette and parameter forms from it, and keeps a committed snapshot of it for when Python isn't available.

```bash
databolt-patterns manifest --out manifest.json              # every pattern
databolt-patterns describe databolt.ingestion.api.rest       # one entry
```

## Top level

```json
{
  "manifest_version": 1,
  "protocol": 1,
  "package": {"name": "databolt-standard-patterns", "version": "0.1.1"},
  "patterns": [ ...entries, sorted by id... ],
  "warnings": [ "entry point 'x' from acme-patterns failed to load: ..." ]
}
```

## Pattern entry

| Key | Meaning |
|---|---|
| `id`, `version` | A stable dotted id and the pattern's own semver version. The package version is separate. |
| `title`, `description`, `category`, `icon` | Display fields. |
| `origin` | `standard` (this package), `package` (another dist's entry point) or `custom` (a YAML or Python file). |
| `provider` | `{name, version}` of the distribution that provides the pattern. |
| `runtime` | Always `python`. |
| `params_schema` | JSON Schema 2020-12 for `params` (see below). |
| `secrets` | `[{name, description, required}]`: the secret names the pattern declares. |
| `defaults` | Defaults for top-level params that have one. |
| `outputs` | `{formats: ["ndjson", "parquet", "duckdb"], record: "object"}` |
| `capabilities` | Free-form flags, e.g. `{"auth": [...], "pagination": [...], "incremental": true}`. |
| `examples` | `[{title, params}]`: ready-to-use params. |

## `params_schema` conventions

The schema is generated from the pattern's Pydantic model:

```python
model_json_schema(by_alias=True, mode="validation")
```

The output carries `"$schema": "https://json-schema.org/draft/2020-12/schema"`, and nested models appear under `$defs`.

- **Secret params** are a reference, never a value. The schema marks them like this:

  ```json
  {"type": "string", "format": "secret-ref", "pattern": "^\\$\\{\\s*secrets\\.[A-Za-z0-9_\\-]+\\s*\\}$",
   "x-databolt-ui": {"widget": "secret-ref"}}
  ```

  The value must look like `${secrets.<name>}`, and a literal secret fails validation. A UI shows a picker of keystore *names*. It writes `${secrets.<name>}` into params and supplies the value only at run time, in the request's `secrets` object.
- **Choices with different shapes** use a discriminated `oneOf`: `{"oneOf": [{"$ref": ...}, ...], "discriminator": {"propertyName": "type", "mapping": {...}}}`. The REST pattern's `auth` and `paginator` work this way. A form renders a selector on `type`, then the chosen variant's fields.
- **`x-databolt-ui`** carries optional rendering hints on a property:
  - `widget`: one of `secret-ref`, `key-value`, `code` (with `language`) or `textarea`;
  - `placeholder`;
  - `group`;
  - `order`;
  - `advanced: true`, which collapses the field by default.

  Unknown hints must be ignored.

## Versioning

- `manifest_version` changes only on a breaking change to this document's shape.
- A pattern's `version` follows semver. A breaking change to its params is a major bump, and Studio pins a range such as `^1`.
- `tests/snapshots/manifest.json` in this repo is the manifest of the built-in patterns. CI fails if it drifts, and you regenerate it on purpose with `UPDATE_SNAPSHOTS=1 pytest`.
