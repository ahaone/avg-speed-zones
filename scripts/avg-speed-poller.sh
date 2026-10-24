#!/bin/sh
# Invoke one bounded poll; serialize concurrent runs and reclaim dead PID locks.
set -eu

REPO_ROOT=${AVG_SPEED_REPO_ROOT:-/opt/avg-speed-zones}
STATE_DIR=${AVG_SPEED_STATE_DIR:-/var/lib/avg-speed-zones}
LOCK_FILE="$STATE_DIR/poller.lock"
PID_FILE="$STATE_DIR/poller.pid"
mkdir -p "$STATE_DIR"

if ! command -v flock >/dev/null 2>&1; then
    printf '%s\n' '{"state":"FAILED","error":"flock is required"}'
    exit 1
fi

# flock releases automatically on crash. Keep the lock inode separate from the
# diagnostic PID file so rewriting stale PID text cannot truncate a held lock.
exec 9>>"$LOCK_FILE"
if ! flock -n 9; then
    printf '%s\n' '{"state":"SKIPPED","reason":"poll already running"}'
    exit 0
fi
printf '%s\n' "$$" >"$PID_FILE"
exec python3 "$REPO_ROOT/tools/poll_bgtoll.py" \
    --repo-root "$REPO_ROOT" \
    --state-dir "$STATE_DIR" \
    "$@"
