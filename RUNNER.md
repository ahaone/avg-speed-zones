# Public runner repo

This repository is the deployment surface, not the development repo.

- Source, tests and tools live in the private `ahaone/avg-speed-zones-dev`.
- This repo holds the workflow, `inbox/`, and the published `zones.json`.
- Required secret: `PRIVATE_REPO_TOKEN` — fine-grained, read-only to the private repo.
- Required repository variable: `V5_PUBLISH_ENABLED` — set to `true` only at cutover.
- No Android builds, APKs, emulators or long test suites run here.
