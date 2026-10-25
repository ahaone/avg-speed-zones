# Public runner repo

This repository is the deployment surface, not the development repo.

- Source, tests and tools live in the private `ahaone/avg-speed-zones-dev`.
- This repo holds the workflow, `inbox/`, and the published legacy `zones.json`.
- v5 publishes to `v5/zones.json`; the legacy `zones.json` stays untouched for v0.2.4.
- Required secret: `PRIVATE_REPO_TOKEN` — fine-grained, read-only to the private repo.
- Required repository variable: `V5_PUBLISH_ENABLED` — set to `true` only at cutover.
- No Android builds, APKs, emulators or long test suites run here.
