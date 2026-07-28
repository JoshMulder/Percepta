# Backup and restore

A backup nobody has restored is a hypothesis. This document exists to be
rehearsed, not read — the restore below has been run, and the check at the end
is the one that tells you whether it worked.

## What a full recovery needs

Three things, and **they must not live in the same place**. Each is useless
without the others, which is the property that makes storing them together a bad
idea rather than a convenient one.

| Thing | Where it is | Lose it and… |
|---|---|---|
| Database dump | `backups/`, from `scripts/backup.sh` | Everything is gone: orgs, users, stations, grants, audit trail, history |
| `SECRETS_ENCRYPTION_KEY` | `.env` | The dump restores, but every encrypted column — TOTP secrets today, more later — is unreadable. The rest of the system works, which makes this easy not to notice until someone tries to use MFA |
| `certs/ca.key` | `certs/` | Every station in the field must be physically re-enrolled. Stations pin this CA; a new one is a new trust root and they will correctly refuse to talk to it |

Not backed up, deliberately:

- **Redis.** Everything in it reconstructs: leases re-acquire, revocations have
  a 60-second poll backstop, and station broker principals are rebuilt from
  Postgres at start-up.
- **Cached map tiles.** Large and refetchable.
- **Station-side data.** Recordings and buffered events live on each station and
  are that station's own retention problem.

## Taking a backup

```bash
cd server && ./scripts/backup.sh
```

Writes `backups/percepta-<timestamp>.sql.gz`, mode 600, keeping the most recent
14. It verifies the dump gunzips and contains a table it knows should be there
before moving it into place, so an interrupted or empty run never leaves
something that looks like a backup and is not.

Prune is by **count, not age**: a stack that has been switched off for a month
still has its last backups when it comes back.

### Scheduling it

Nothing schedules this yet — see *Still outstanding*. On the host, hourly:

```bash
0 * * * * cd /home/percepta/percepta/server && ./scripts/backup.sh >> /var/log/percepta-backup.log 2>&1
```

## Restoring

```bash
# 1. Stop the application so nothing writes while you work.
docker compose stop app

# 2. Restore into a scratch database FIRST and look at it. Restoring straight
#    over production is how a good backup and a bad one become indistinguishable.
docker compose exec -T postgres psql -U percepta -d postgres \
  -c 'DROP DATABASE IF EXISTS restore_test;' -c 'CREATE DATABASE restore_test;'
gunzip -c backups/percepta-<timestamp>.sql.gz \
  | docker compose exec -T postgres psql -q -U percepta -d restore_test

# 3. Check it is what you expect, in the terms you actually care about.
docker compose exec -T postgres psql -U percepta -d restore_test -c \
  "select (select count(*) from organizations) orgs,
          (select count(*) from users) users,
          (select count(*) from ground_stations) stations,
          (select count(*) from station_credentials) credentials,
          (select max(created_at) from audit_logs) last_audit_entry"

# 4. Only then, over the real database.
docker compose exec -T postgres psql -U percepta -d postgres \
  -c 'DROP DATABASE percepta;' -c 'CREATE DATABASE percepta;'
gunzip -c backups/percepta-<timestamp>.sql.gz \
  | docker compose exec -T postgres psql -q -U percepta -d percepta

# 5. Bring it back. Migrations and broker principals are applied at start-up.
docker compose start app
docker compose exec -T postgres psql -U percepta -d postgres -c 'DROP DATABASE restore_test;'
```

`last_audit_entry` is the useful number in step 3. Row counts tell you the dump
is not empty; the timestamp tells you *how much you lost*, which is the question
someone will actually be asking at the time.

## After a restore

**Stations keep working**, provided the CA is the same one. Their credentials
are rows in the dump, and the platform rebuilds each broker principal from those
rows at start-up. Nothing needs re-enrolling.

If the CA was lost and regenerated, they do **not** keep working and cannot be
fixed remotely — each station pins the old CA and will correctly refuse the new
one. That is a site visit per station, and it is the single most expensive thing
in this document.

Check afterwards that stations came back:

```bash
docker compose exec -T postgres psql -U percepta -d percepta -c \
  "select name, last_seen_at, now() - last_seen_at as silent_for
     from ground_stations where is_active order by last_seen_at nulls first"
```

## Still outstanding

- **Nothing runs this automatically.** The script exists and works; no cron, no
  timer, no alert if it stops producing files. A backup script nobody has
  scheduled is a backup nobody has.
- **Backups stay on the same host as the database.** That covers a bad migration
  and a dropped table. It does not cover the disk, the machine, or the building.
- **No restore drill on a schedule.** This has been rehearsed once, by hand, on
  the day it was written.
