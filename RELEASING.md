# Releasing

Releases are cut from `main` by pushing a tag. The tag must equal `v` + the version in
`src/databolt/patterns/_version.py` (for example `v0.1.1`, or `v0.2.0a1` for a pre-release).

```bash
# 1. bump src/databolt/patterns/_version.py and CHANGELOG.md in a PR; merge it with CI green
# 2. tag the merge commit
git checkout main && git pull
git tag -a v0.1.1 -m "v0.1.1"
git push origin v0.1.1
```

The `Release` workflow then:

1. builds the sdist and wheel, runs `twine check`, and fails if the wheel ships `databolt/__init__.py`;
2. creates a GitHub release with the artifacts attached (tags with `a`, `b` or `rc` are marked pre-release);
3. publishes to **TestPyPI**, then to **PyPI**, through trusted publishing (OIDC, no API tokens).

Step 3 runs only when the repository variable `PYPI_PUBLISH` is `true`. Without it, the
publish jobs are skipped with a notice and the workflow stays green.

## One-time setup (a maintainer with PyPI and repo-admin access)

Do these once, before the first publishing release.

1. **GitHub environments.** In *Settings → Environments*, create:
   - `testpypi` (no protection needed);
   - `pypi` with **Required reviewers** set to the maintainers, so every PyPI upload waits for an approval.
2. **TestPyPI trusted publisher.** On <https://test.pypi.org/manage/account/publishing/>, add a
   *pending publisher*:
   - PyPI project name: `databolt-standard-patterns`
   - Owner: `DataBlock-Labs`
   - Repository: `databolt-standard-patterns`
   - Workflow: `release.yml`
   - Environment: `testpypi`
3. **PyPI trusted publisher.** Same form on <https://pypi.org/manage/account/publishing/>, with
   environment `pypi`.
4. **Turn publishing on.** In *Settings → Secrets and variables → Actions → Variables*, add
   `PYPI_PUBLISH` = `true`.

A pending publisher creates the project on its first upload, which also claims the name.

Optional: whether to also reserve the bare `databolt` name on PyPI (for a future core
distribution that shares the `databolt` namespace) is a separate decision. This package
deliberately does not claim it, and it never ships `databolt/__init__.py`.

## Re-running a release

If publishing was skipped because the setup was not done yet, finish the setup and then
re-run the failed or skipped jobs from the workflow run page. Re-running the whole workflow
fails at the GitHub-release step because the release already exists; delete the release
(not the tag) first if you need a full re-run. TestPyPI uploads use `skip-existing`.

## Installing without PyPI

Every tag is installable straight from git:

```bash
pip install "databolt-standard-patterns @ git+https://github.com/DataBlock-Labs/databolt-standard-patterns@v0.1.1"
```
