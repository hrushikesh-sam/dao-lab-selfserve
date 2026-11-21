# deploy/backup — nightly DB backup (B-101)

`backup-db.sh` takes one sqlite online backup of `DAOLAB_DB_PATH` (safe while the API and the
engine run), checks the copy with `PRAGMA integrity_check`, writes it as
`backups/daolab-<UTC time>.db`, and deletes backups older than `DAOLAB_BACKUP_KEEP_DAYS` (14).
`daolab-backup.timer` runs it every night at 02:30 (plus up to 15 min), and catches up after
downtime (`Persistent=true`).

## Install (on the daolab server, as root)

```
cp /opt/daolab/deploy/backup/daolab-backup.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now daolab-backup.timer
```

Verify:

```
systemctl start daolab-backup.service                # one backup now
systemctl status daolab-backup.service --no-pager    # "backup: /opt/daolab/backups/daolab-...db"
systemctl list-timers daolab-backup.timer            # the next run
ls -l /opt/daolab/backups/
```

## Restore

The API and the engine must be stopped: the DB is replaced underneath them.

```
systemctl stop daolab-api.service daolab-engine.timer
cd /opt/daolab
cp daolab.db daolab.db.before-restore                # keep what is being replaced
cp backups/daolab-<UTC time>.db daolab.db
rm -f daolab.db-wal daolab.db-shm                     # stale WAL files belong to the old DB
chown daolab: daolab.db
sudo -u daolab .venv/bin/daolab init                  # applies any newer migrations; no-op otherwise
systemctl start daolab-api.service daolab-engine.timer
```

Reservations made after the backup was taken are lost; anything they left on the testbed is
found by the reconciler's orphan sweep (B-73) and cleaned.
