#!/bin/sh
# Start the existing Reader deployment from the owner's @reboot crontab.
set -eu
umask 077

reader_script_dir=$(/usr/bin/dirname -- "$0")
reader_project_dir=$(cd "$reader_script_dir/.." && pwd -P)
reader_runtime_dir="$reader_project_dir/runtime"
/usr/bin/mkdir -p "$reader_runtime_dir"

# One invocation at a time; keep the descriptor open until this script exits.
exec 9>"$reader_runtime_dir/boot-start.lock"
/usr/bin/flock -n 9 || exit 0

reader_boot_log="$reader_runtime_dir/boot.log"
if [ -f "$reader_boot_log" ] && [ "$(/usr/bin/stat -c %s "$reader_boot_log")" -ge 1048576 ]; then
    /usr/bin/mv -f -- "$reader_boot_log" "$reader_boot_log.1"
fi
exec >>"$reader_boot_log" 2>&1
/usr/bin/date -u '+%Y-%m-%dT%H:%M:%SZ Starting Reader boot check'

# Cron may run before the Docker socket is ready. Bound the wait and each probe.
reader_docker_deadline=$(( $(/usr/bin/date +%s) + 180 ))
until /usr/bin/timeout --kill-after=2s 5s /usr/bin/docker info >/dev/null 2>&1; do
    if [ "$(/usr/bin/date +%s)" -ge "$reader_docker_deadline" ]; then
        echo 'Docker was not available within 180 seconds; check docker.service and user permissions.'
        exit 1
    fi
    /usr/bin/sleep 2
done

# Start only the saved Reader services and their volume initializer. Existing containers
# are kept; upgrades, model provisioning and owner administration remain explicit.
if /usr/bin/timeout --kill-after=10s 240s /usr/bin/docker compose \
    --project-directory "$reader_project_dir" \
    --file "$reader_project_dir/compose.yaml" --project-name reader-onion \
    up --detach --no-build --no-recreate --pull never --wait --wait-timeout 180 \
    reader-tor reader; then
    /usr/bin/date -u '+%Y-%m-%dT%H:%M:%SZ Reader and Tor are healthy'
else
    reader_start_status=$?
    echo "Reader boot health check failed (exit $reader_start_status); inspect docker compose ps/logs."
    exit "$reader_start_status"
fi
