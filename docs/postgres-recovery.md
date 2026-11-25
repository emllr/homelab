# Postgres: recovery that doesn't depend on the hypervisor

**Goal:** if the hypervisor or its storage is lost, recover the database from the NAS alone.

| Layer | Covers |
|---|---|
| pgBackRest, local repository | quick restores and point-in-time recovery |
| pgBackRest, second repository on the NAS (SFTP, encrypted) | loss of the hypervisor |
| Asynchronous streaming replica on the NAS | minutes-old copy, quick promotion |
| Read-only btrfs snapshots of the NAS repository | deleted or corrupted backups |
| Nightly `pg_dump` | logical damage, version-independent copy |

## Details that mattered
- **Collation:** the replica's image must use the same glibc collation as the primary. Alpine's libc sorts text
  differently, which corrupts replicated text indexes without any error.
- **Slot limit:** a replication slot keeps WAL while the replica is away. `max_slot_wal_keep_size` caps it, so a
  long NAS outage costs the replica, not the primary's disk.
- **WAL archiving:** with two repositories, a segment stays in `pg_wal` until both have it, so an outage of the second
  one also uses disk on the primary.
- **Start order:** Postgres started before the backup disk was mounted at boot, and archiving failed until it was.
  A systemd drop-in now orders it after the mount.
- **Alert on missing success:** the first snapshot alert fired only on a reported failure, so a stopped timer was
  silent. It now fires when no success has been seen.
- **Encryption has limits here:** the passphrase and an unencrypted replica are on the same NAS, so the
  repository's encryption adds little against someone with root there.

## Tested, and not
Tested: a restore on the NAS using only its own data (about 4 minutes for a 35 GB database, row counts matched),
an outage of both NAS containers (recovered unaided), snapshot rotation, and the snapshot alert end to end.
Not yet tested: a NAS reboot, point-in-time recovery, and restoring from a snapshot.

Code: [`roles/postgres_server`](../roles/postgres_server), [`tools/nas1-postgres`](../tools/nas1-postgres).
