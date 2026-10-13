# Shipping patterns in a package

Any installed distribution can contribute patterns through the **`databolt.patterns`** entry-point group. That is the same group the standard patterns use.

```toml
# pyproject.toml
[project]
name = "acme-databolt-patterns"
dependencies = ["databolt-standard-patterns>=0.1,<0.2"]

[project.entry-points."databolt.patterns"]
orders = "acme_patterns.orders:orders"      # a @pattern function
billing = "acme_patterns.billing:Billing"   # a Pattern subclass
more = "acme_patterns:PATTERNS"              # or a list/tuple of either
```

After `pip install acme-databolt-patterns`, the patterns show up in `databolt-patterns list` with `origin: package`, and `provider` is set to the distribution's name and version. In DataBolt Studio, install them with `databolt-studio python pip install acme-databolt-patterns` and refresh the palette.

## Rules

- **Pattern ids are global.** Prefix them with your namespace, for example `acme.orders`. If an id is already registered, the second registration is ignored and a warning is added to the manifest.
- **A broken package doesn't break the palette.** If an entry point fails to import, it becomes a manifest `warning` instead of an error.
- **Don't ship `databolt/__init__.py`.** If you want your modules under the `databolt` namespace (for example `databolt/ingestion/acme/`), use a PEP 420 namespace package. Most packages should simply use their own top-level name.
- **Version your patterns with semver.** Studio pins a range, such as `^1`, per pipeline.

A working example lives in [`examples/entrypoint_pkg`](../../examples/entrypoint_pkg). CI installs it and checks that both of its patterns are discovered.
