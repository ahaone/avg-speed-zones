# Inbox contract

The phone collector writes only changed snapshots here.

- `bgtoll-wim.html` — raw BGTOLL WIM page
- `zones-live.kml` — raw BGTOLL Google My Maps KML

A push to `inbox/**` starts `v5-collect-publish`. The workflow checks out the
private source repo, re-parses the raw table, verifies it, and publishes only
when `V5_PUBLISH_ENABLED` is `true`.

Do not commit credentials, tokens or private notes here. Prune processed
snapshots after publication so the public repo does not grow indefinitely.
