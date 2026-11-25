#!/bin/bash
# Run on nas1 as root: restore.sh DEST_DIR [extra pgbackrest restore options]
# Restores the newest backup in ./repo to an empty DEST_DIR and starts it (container db1-restore, no network).
# For a point in time: restore.sh DEST --type=time --target="2026-11-25 02:00:00+00"
set -euo pipefail
DEST=$1; shift
cd "$(dirname "$0")"; D=/usr/local/bin/docker; P=/root/pgbackrest-repo2-passphrase.txt
[ -e "$DEST" ] && { echo "$DEST exists; give a new directory" >&2; exit 1; }
[ "$#" -gt 0 ] || set -- --type=immediate
mkdir "$DEST"; chattr +C "$DEST"; mkdir -p "$DEST/main" "$DEST/log"
{ printf '[global]\nrepo1-path=/repo\nrepo1-cipher-type=aes-256-cbc\nrepo1-cipher-pass='; cat "$P"
  printf '\nprocess-max=4\nlock-path=/tmp\nlog-path=/work/log\n[exampleapp]\npg1-path=/work/main\n'; } > "$DEST/pgbackrest.conf"
chmod 600 "$DEST/pgbackrest.conf"; cp conf/postgresql.conf "$DEST/pg.conf"
sed -i "s#/var/lib/postgresql/18/main#/work/main#; s#/etc/pg/pg_hba.conf#/work/hba.conf#; /ident_file/d" "$DEST/pg.conf"
echo 'local all postgres trust' > "$DEST/hba.conf"
$D build -q -t db1-restore:1 restore >/dev/null
# The repo belongs to uid 2990 (the SFTP user): restore as root, then hand the cluster to postgres (999).
$D run --rm --network none -v "$DEST:/work" -v "${REPO:-$PWD/repo}:/repo:ro" --entrypoint bash db1-restore:1 -c '
  pgbackrest --config=/work/pgbackrest.conf --stanza=exampleapp --log-level-console=warn --target-action=promote restore "$@" &&
  chown -R 999:999 /work && chmod 700 /work/main' _ "$@"
# archive-get runs as postgres, so it gets read access to the repo through group 2990 (the repo is 0750).
$D run -d --name db1-restore --network none --user 999:999 --group-add 2990 -e PGDATA=/work/main -v "$DEST:/work" -v "${REPO:-$PWD/repo}:/repo:ro" \
  --shm-size 1g --memory 10g db1-restore:1 postgres -c config_file=/work/pg.conf
echo "started; check: docker logs db1-restore; docker exec db1-restore psql -U postgres exampleapp"
