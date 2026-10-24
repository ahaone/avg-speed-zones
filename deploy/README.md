# Avg speed zone poller (VPS/systemd)

This is a **VPS/systemd scheduler**, not GitHub Actions. The service runs one bounded tick; the timer provides the nominal five-minute cadence. No service or timer was installed or enabled by this change.

## Install paths

Deploy the repository at `/opt/avg-speed-zones` and install the unit files from this directory into `/etc/systemd/system/`:

```sh
sudo install -d -m 0755 /opt/avg-speed-zones
sudo install -m 0644 deploy/avg-speed-zones-poller.service /etc/systemd/system/
sudo install -m 0644 deploy/avg-speed-zones-poller.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/avg-speed-zones-poller.service /etc/systemd/system/avg-speed-zones-poller.timer
```

The service uses systemd's unprivileged transient `DynamicUser`, has its private writable `StateDirectory` at `/var/lib/avg-speed-zones`, and reads optional operator settings from `/etc/avg-speed-zones.env`. Keep credentials out of the repository; this poller currently requires no credentials. The wrapper's `poller.lock` flock inode is separate from the diagnostic `poller.pid`, so stale lock state is reclaimed automatically when the owning process exits. Do not enable the timer until a source host/access path is deliberately approved and measured.

## Logs and heartbeat

- One-line JSON stdout/stderr is available with `journalctl -u avg-speed-zones-poller.service`.
- Persistent heartbeat, source snapshots, semantic freshness state, and backoff/request history live under `/var/lib/avg-speed-zones/`; inspect `poller-status.json` or run `scripts/avg-speed-poller.sh --status`.
- `last_success` is the reviewed-baseline WIM freshness success. A blocked or malformed fetch leaves last-good data and this success pointer unchanged. The map digest is only a change signal and cannot silently override the official WIM source.

To check the timer and its next/previous activation:

```sh
systemctl list-timers avg-speed-zones-poller.timer
systemctl status avg-speed-zones-poller.timer avg-speed-zones-poller.service
```

## Local dry-run smoke

No live source, publish, or service install is needed to exercise the offline path:

```sh
state_dir="$(mktemp -d)"
python3 tools/poll_bgtoll.py \
  --repo-root "$PWD" \
  --state-dir "$state_dir" \
  --baseline data/bgtoll-vaprosi-i-otgovori.html \
  --map-file tests/fixtures/poller-map.kml \
  --source-file data/bgtoll-vaprosi-i-otgovori.html
```

The command emits one JSON line, records local-only snapshots and status in the temporary directory, and will report `HELD` if verifier/candidate evidence is incomplete. It does not write `zones.json` without `--publish` and every publish gate passing. Remove the temporary directory when finished.
