#!/bin/bash
# Copies the compose project to nas1 (rsync/sftp to DSM don't work, so tar over ssh). Doesn't touch data/ or repo/.
set -euo pipefail
cd "$(dirname "$0")"
DEST=/srv/db1-postgres
tar -c compose.yml conf sftp restore restore.sh snapshot.sh systemd | ssh -o BatchMode=yes root@10.50.1.114 "
  set -e; mkdir -p $DEST && chmod 700 $DEST && cd $DEST
  tar -x --no-same-owner
  if [ ! -d data ]; then mkdir data; chattr +C data 2>/dev/null || echo 'chattr +C not supported'; fi
  [ -d repo ] || btrfs subvolume create repo
  mkdir -p data/18 sftp-state snapshots status; chmod 700 snapshots; chmod 755 status
  chmod 700 data; chmod 755 sftp-state; chown -R 999:999 data/18; chown 999:999 data
  chown 2990:2990 repo; chmod 750 repo
  chmod -R a+rX conf
  cp systemd/db1-repo-snapshot.* /etc/systemd/system/; chmod 644 /etc/systemd/system/db1-repo-snapshot.*
  systemctl daemon-reload; systemctl enable db1-repo-snapshot.timer; systemctl restart db1-repo-snapshot.timer"
