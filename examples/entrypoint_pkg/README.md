# databolt-example-patterns

A minimal package that ships DataBolt patterns through the `databolt.patterns` entry-point group.

```bash
pip install -e examples/entrypoint_pkg
databolt-patterns list          # example.countdown and example.static_rows, origin "package"
databolt-patterns run example.countdown --param start=5 --preview
```

See [docs/authoring/package.md](../../docs/authoring/package.md).
