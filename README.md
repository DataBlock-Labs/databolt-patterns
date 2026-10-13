# databolt-standard-patterns

Open-source ingestion patterns for the DataBolt data platform.

`databolt.ingestion.api` is a standard library of REST ingestion patterns. Each pattern handles the hard parts of an API pull for you: auth, pagination, retries and rate limits, and incremental cursors. Its sibling `databolt.patterns` is the small framework for writing your own patterns.

This repository is being bootstrapped; the package lands in follow-up pull requests.

Licensed under Apache-2.0.
