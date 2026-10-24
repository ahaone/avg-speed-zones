# Avg Speed Zones (server-side validation)

This repository contains the source inventory, verifier, candidate builder, fail-closed publisher, and bounded BGTOLL poller. The poller is a server/VPS systemd workload, **not GitHub Actions**.

## Poller cadence

- Tier 1 reads the official Google-hosted map KML at a nominal five-minute interval and hashes normalized placemark semantics (folders, names, descriptions, geometry), not volatile XML wrapper bytes.
- Tier 2 reads the BGTOLL WIM table only after a stable Tier-1 semantic change or when the last valid WIM fetch reaches its daily age. Tier 2 is capped at two requests per rolling 24 hours with at least five minutes between requests, a 120-second map-change debounce, and persistent 15m/30m/1h/2h/6h failure backoff. A tick during backoff makes no HTTP request and exits 3.
- HTTP is performed at most once per source per tick. Cloudflare 403/429 and `Attention Required!` are treated as `SOURCE_BLOCKED`; the WIM capture and last-good pointers are not advanced. No retry loop exists.
- Source and candidate changes are dry-run by default. `zones.json` can only be replaced with explicit `--publish` plus a complete, gate-clean candidate; source changes still require review.

## Dry-run smoke

Run from the repository root with Python 3:

```sh
state_dir="$(mktemp -d)"
python3 tools/poll_bgtoll.py \
  --repo-root "$PWD" \
  --state-dir "$state_dir" \
  --baseline data/bgtoll-vaprosi-i-otgovori.html \
  --map-file tests/fixtures/poller-map.kml \
  --source-file data/bgtoll-vaprosi-i-otgovori.html
```

This uses only local fixture files, performs no HTTP requests, writes status/snapshots only under the temporary directory, and never publishes by default. The first offline tick seeds map and WIM semantic state; later unchanged runs report no change. Remove the temporary directory afterward.

See [`deploy/README.md`](deploy/README.md) for systemd installation paths, heartbeat/log locations, and timer verification. This implementation does not install/enable the units or claim a live VPS source-access probe.
