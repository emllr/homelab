#!/bin/bash
# Daily read-only btrfs snapshot of the pgBackRest repo (a subvolume) on nas1; keeps the newest 14.
# db1's SFTP key is chrooted to repo/ and can't reach snapshots/. Old ones go only after a new one exists.
set -euo pipefail
export PATH=/sbin:/bin:/usr/sbin:/usr/bin
cd /srv/db1-postgres; KEEP=14
n=snapshots/repo-$(date -u +%Y%m%dT%H%M%SZ)
# An emptied repo must not push good snapshots out of rotation.
[ -f repo/backup/exampleapp/backup.info ] || { logger -p user.err "db1 repo snapshot: backup.info missing, nothing taken or pruned"; exit 1; }
btrfs subvolume snapshot -r repo "$n"
ls -d snapshots/repo-*Z | sort -r | tail -n +$((KEEP + 1)) | while read -r s; do btrfs subvolume delete "$s"; done
# For db1's hourly check (alert "nas1 backup snapshots stale"): time of this snapshot, how many exist.
echo "$(date -u +%s) $(ls -d snapshots/repo-*Z | wc -l) ${n#snapshots/}" > status/.new
chmod 644 status/.new; mv status/.new status/last-snapshot
logger "db1 repo snapshot: $n"
